"""
TabiLog Bot - 世界一周の費用ログ用Discord Bot

コマンド一覧:
  /log_ex      支出を記録（金額入力→タグ/割り勘を1画面で選択）
  /log_in      予算を記録（金額入力→タグを1画面で選択）
  /log_memo    自分用の買い物メモを記録
  /log_memo_ls 自分の買い物メモ一覧（自分だけに表示）
  /log_memo_rm 自分の買い物メモを削除
  /log_ct      このスレッド/チャンネルを国に紐付け
  /log_tag_add タグを登録
  /log_tag_rm  タグを削除
  /log_tag_ls  登録タグ一覧
  /log_sum     国・タグで絞り込み集計（自分だけに表示）
  /log_all     全体サマリー
  /log_ls      ID付き一覧表示（自分だけに表示）
  /log_ed      IDを指定して記録を確認・修正
  /log_rm      IDを複数指定して削除
  /log_wari    割り勘の精算計算（メンションあり）
"""
import asyncio
import datetime
import os
import re
import logging
import unicodedata
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

import fx
import countries
from sheets import SheetStore

load_dotenv()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tabilog")

DEFAULT_COUNTRY = "不明"  # 国が紐付いていないチャンネル用のデフォルト
# /log_sum で国指定時、このタグ付き記録は集計から除外（tag=両替 の明示指定時は除外しない）
SUM_COUNTRY_EXCLUDE_TAG = "両替"
# 種別「予算」。旧データ互換のため「収入」も予算として扱う
ENTRY_TYPE_EXPENSE = "支出"
ENTRY_TYPE_BUDGET = "予算"
ENTRY_TYPE_BUDGET_ALIASES = frozenset({ENTRY_TYPE_BUDGET, "収入"})
DISCORD_ID_RE = re.compile(r"\d{17,20}")
MENTION_RE = re.compile(r"<@!?(\d{17,20})>")


def _load_default_participant_ids() -> list[str]:
    """DEFAULT_SPLIT_PARTICIPANT_IDS からデフォルト参加者IDを読む（カンマ/空白区切り）。"""
    raw = os.environ.get("DEFAULT_SPLIT_PARTICIPANT_IDS", "")
    ids = []
    for token in re.split(r"[\s,、]+", raw):
        token = token.strip()
        if DISCORD_ID_RE.fullmatch(token) and token not in ids:
            ids.append(token)
    # UserSelect の max_values に合わせて上限を切る
    return ids[:10]


DEFAULT_SPLIT_PARTICIPANT_IDS = _load_default_participant_ids()

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

store: SheetStore | None = None


async def _exclude_bot_user_ids(user_ids: list[str]) -> list[str]:
    """Botアカウントを除外したユーザーIDを返す。取得失敗時は元のIDを残す。"""
    human_ids = []
    for user_id in user_ids:
        user = bot.get_user(int(user_id))
        if user is None:
            try:
                user = await bot.fetch_user(int(user_id))
            except discord.HTTPException:
                user = None
        if user is None or not user.bot:
            human_ids.append(user_id)
    return human_ids


def get_store() -> SheetStore:
    global store
    if store is None:
        store = SheetStore()
    return store


async def run_sync(func, *args, **kwargs):
    """同期のSheets/HTTP呼び出しをスレッドに逃がし、イベントループを塞がない。"""
    return await asyncio.to_thread(func, *args, **kwargs)


async def aget_store() -> SheetStore:
    """Sheets 初期化をスレッドで行い、イベントループを塞がない。"""
    return await run_sync(get_store)


async def sheets(method_name: str, *args, **kwargs):
    """SheetStore の同期メソッドをスレッド上で呼ぶ。"""

    def _call():
        return getattr(get_store(), method_name)(*args, **kwargs)

    return await run_sync(_call)


async def _safe_defer(
    interaction: discord.Interaction, *, ephemeral: bool = False
) -> bool:
    """defer する。期限切れ・二重応答なら False（呼び出し側は処理を中断）。"""
    if interaction.response.is_done():
        return True
    try:
        await interaction.response.defer(ephemeral=ephemeral)
        return True
    except discord.NotFound:
        log.warning("interaction expired before defer (event loop blocked or too slow)")
        return False
    except discord.HTTPException as e:
        # 40060: Interaction has already been acknowledged
        if getattr(e, "code", None) == 40060:
            log.warning(
                "interaction already acknowledged "
                "(duplicate bot, double-click, or delayed handler)"
            )
            return False
        raise


async def _prefetch_country_default(channel_id: int | str | None) -> str:
    """モーダル用の国デフォルトを非同期取得（失敗・遅延時は不明）。"""
    if channel_id is None:
        return DEFAULT_COUNTRY
    try:
        binding = await asyncio.wait_for(
            sheets("get_channel_binding", channel_id),
            timeout=1.5,
        )
        if binding and str(binding.get("国", "")).strip():
            return str(binding["国"]).strip()
    except asyncio.TimeoutError:
        log.warning("prefetch channel country timed out; using default")
    except Exception:
        log.exception("failed to prefetch channel country")
    return DEFAULT_COUNTRY


def _valid_discord_id(value) -> str | None:
    """保存値が正しいDiscordユーザーIDなら返す。壊れた数値は除外する。"""
    cleaned = str(value).strip().lstrip("'")
    return cleaned if DISCORD_ID_RE.fullmatch(cleaned) else None


def _participant_ids(entry: dict) -> list[str]:
    values = str(entry.get("参加者ID", "")).split(",")
    return [uid for value in values if (uid := _valid_discord_id(value))]


def _resolve_currency(
    country: str,
    binding: dict | None,
    manual_input: str,
) -> str:
    """手入力 > /log_ct の保存通貨 > /log_ct の国名推定 > JPY。

    ct 未設定かつ通貨未入力なら、フォームの国名があっても換算せず JPY。
    """
    manual = (manual_input or "").strip().upper()
    if manual:
        return manual
    if not binding:
        return "JPY"
    stored = str(binding.get("通貨", "")).strip().upper()
    if stored:
        return stored
    ct_country = str(binding.get("国", "")).strip() or country
    return countries.get_currency_for_country(ct_country) or "JPY"


def _parse_amount(value: str) -> float:
    """金額文字列を float に変換する。カンマ区切りや全角小数点にも対応。"""
    text = str(value).strip()
    text = text.replace("，", "").replace(",", "").replace("．", ".")
    text = text.replace("￥", "").replace("¥", "").replace(" ", "")
    if not text:
        raise ValueError("empty amount")
    amount = float(text)
    if amount < 0:
        raise ValueError("negative amount")
    return amount


def _format_amount(amount: float | int | str, *, decimals: int | None = None) -> str:
    """表示用金額。整数は小数なし、それ以外は必要な桁だけ出す。"""
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return str(amount)
    if decimals is not None:
        formatted = f"{value:,.{decimals}f}"
        return formatted.rstrip("0").rstrip(".") if decimals > 0 else formatted
    if value.is_integer():
        return f"{value:,.0f}"
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def _resolve_country(binding: dict | None, manual_input: str) -> str:
    """/log_ct 設定があればそれを優先。なければ手入力、空なら不明。"""
    if binding and str(binding.get("国", "")).strip():
        return str(binding["国"]).strip()
    manual = (manual_input or "").strip()
    return manual or DEFAULT_COUNTRY


def _entry_embed(
    entry_id: int,
    entry_type: str,
    country: str,
    amount_local: float,
    currency: str,
    amount_jpy: float,
    tags: str,
    memo: str,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"✅ 記録しました (ID: {entry_id})",
        color=discord.Color.green() if entry_type in ENTRY_TYPE_BUDGET_ALIASES else discord.Color.orange(),
    )
    embed.add_field(name="種別", value=_display_entry_type(entry_type), inline=True)
    embed.add_field(name="国", value=country, inline=True)
    if currency != "JPY":
        embed.add_field(
            name="金額",
            value=f"{_format_amount(amount_local)} {currency} → 約{_format_amount(amount_jpy)}円",
            inline=False,
        )
    else:
        embed.add_field(
            name="金額",
            value=f"{_format_amount(amount_jpy)}円",
            inline=False,
        )
    if tags:
        embed.add_field(name="タグ", value=tags, inline=True)
    if memo:
        embed.add_field(name="メモ", value=memo, inline=False)
    if country == DEFAULT_COUNTRY:
        embed.set_footer(
            text="国が未設定です。フォームの国欄に入力するか、/log_ct でスレッドに国を固定できます。"
        )
    return embed


# ---------------------------------------------------------------------------
# 支出/予算 記録用モーダル → オプションを1画面でまとめて選択
# ---------------------------------------------------------------------------

class EntryModal(discord.ui.Modal):
    def __init__(self, entry_type: str, country_default: str = DEFAULT_COUNTRY):
        super().__init__(title=f"{entry_type}を記録")
        self.entry_type = entry_type

        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

        self.amount = discord.ui.TextInput(
            label="金額",
            placeholder="例: 250 または 12.5",
            required=True,
            max_length=20,
        )
        self.currency = discord.ui.TextInput(
            label="通貨（省略時は /log_ct の通貨）",
            placeholder="空欄: ct設定の通貨、未設定なら円",
            required=False,
            max_length=10,
        )
        self.country = discord.ui.TextInput(
            label="国（任意。/log_ct 設定時はその国を優先）",
            default=country_default,
            placeholder="例: タイ（未入力なら不明）",
            required=False,
            max_length=40,
        )
        self.recorded_at = discord.ui.TextInput(
            label="日時 (YYYY-MM-DD HH:MM)",
            default=now,
            placeholder="例: 2026-07-25 18:30",
            required=True,
            max_length=19,
        )
        self.memo = discord.ui.TextInput(
            label="メモ", required=False, max_length=200
        )

        self.add_item(self.amount)
        self.add_item(self.currency)
        self.add_item(self.country)
        self.add_item(self.recorded_at)
        self.add_item(self.memo)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount_local = _parse_amount(self.amount.value)
        except ValueError:
            await interaction.response.send_message(
                f"金額は数字で入力してください（小数点可。入力値: {self.amount.value}）",
                ephemeral=True,
            )
            return

        recorded_at = _parse_datetime(self.recorded_at.value)
        if not recorded_at:
            await interaction.response.send_message(
                "日時は `YYYY-MM-DD HH:MM` 形式で指定してください（例: `2026-07-25 18:30`）。",
                ephemeral=True,
            )
            return

        # Sheets/為替は数秒かかるので先に応答を確保する
        await interaction.response.defer()

        st = get_store()
        channel_id = str(interaction.channel_id)

        try:
            # スレッド単位で国設定を参照（親チャンネルにはフォールバックしない）
            binding = await run_sync(st.get_channel_binding, channel_id)
            country = _resolve_country(binding, self.country.value or "")
            currency = _resolve_currency(
                country,
                binding,
                self.currency.value or "",
            )
            amount_jpy, rate = await run_sync(fx.convert_to_jpy, amount_local, currency)
        except Exception as e:
            await interaction.followup.send(
                f"為替レートの取得に失敗しました: {e}\n通貨コードを確認してください。",
                ephemeral=True,
            )
            return

        memo = self.memo.value.strip()

        try:
            # タグ・割り勘は次の1画面で選ぶので、いったん空/OFFで保存する
            entry_id = await run_sync(
                st.add_entry,
                entry_type=self.entry_type,
                channel_id=channel_id,
                country=country,
                amount_local=amount_local,
                currency=currency,
                rate=rate,
                amount_jpy=amount_jpy,
                tags="",
                memo=memo,
                split=False,
                payer_id="",
                participants="",
                recorded_at=recorded_at,
            )
            tags = await run_sync(st.list_tags)
        except Exception as e:
            log.exception("entry submit failed")
            await interaction.followup.send(
                f"記録に失敗しました: {e}\n少し待ってからもう一度送信してください。",
                ephemeral=True,
            )
            return

        embed = _entry_embed(
            entry_id, self.entry_type, country, amount_local, currency, amount_jpy, "", memo
        )
        embed.insert_field_at(1, name="日時", value=recorded_at, inline=True)
        await interaction.followup.send(embed=embed)

        if self.entry_type == "支出":
            guide = (
                f"ID {entry_id}: 下でまとめて選んでから確定してください。\n"
                "・タグ（任意）\n"
                "・割り勘あり → 支払者と参加者を選んで「割り勘ありで確定」\n"
                "・集計対象にしたい場合は「集計対象: OFF」ボタンで ON に切り替え"
            )
            if DEFAULT_SPLIT_PARTICIPANT_IDS:
                guide += "\n※参加者は既定メンバーを選択済み（変更可）"
        else:
            guide = (
                f"ID {entry_id}: タグを選んで「確定」を押してください"
                "（集計対象にしたい場合はボタンで ON に切り替え）。"
            )

        default_participant_ids = []
        if self.entry_type == "支出":
            default_participant_ids = await _exclude_bot_user_ids(
                DEFAULT_SPLIT_PARTICIPANT_IDS
            )

        await interaction.followup.send(
            content=guide,
            view=EntryOptionsView(
                entry_id=entry_id,
                entry_type=self.entry_type,
                tags=tags,
                default_participant_ids=default_participant_ids,
            ),
        )


class MemoModal(discord.ui.Modal):
    """ユーザー固有の買い物メモ（共有ログとは別管理）。"""

    def __init__(self, country_default: str = DEFAULT_COUNTRY):
        super().__init__(title="買い物メモを記録")
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        self.amount = discord.ui.TextInput(
            label="金額",
            placeholder="例: 250 または 12.5",
            required=True,
            max_length=20,
        )
        self.currency = discord.ui.TextInput(
            label="通貨（省略時は /log_ct の通貨）",
            placeholder="空欄: ct設定の通貨、未設定なら円",
            required=False,
            max_length=10,
        )
        self.country = discord.ui.TextInput(
            label="国（任意。/log_ct 設定時はその国を優先）",
            default=country_default,
            placeholder="例: タイ（未入力なら不明）",
            required=False,
            max_length=40,
        )
        self.recorded_at = discord.ui.TextInput(
            label="日時 (YYYY-MM-DD HH:MM)",
            default=now,
            placeholder="例: 2026-07-25 18:30",
            required=True,
            max_length=19,
        )
        self.memo = discord.ui.TextInput(
            label="メモ",
            placeholder="例: お土産候補",
            required=False,
            max_length=200,
        )
        self.add_item(self.amount)
        self.add_item(self.currency)
        self.add_item(self.country)
        self.add_item(self.recorded_at)
        self.add_item(self.memo)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount_local = _parse_amount(self.amount.value)
        except ValueError:
            await interaction.response.send_message(
                f"金額は数字で入力してください（小数点可。入力値: {self.amount.value}）",
                ephemeral=True,
            )
            return

        recorded_at = _parse_datetime(self.recorded_at.value)
        if not recorded_at:
            await interaction.response.send_message(
                "日時は `YYYY-MM-DD HH:MM` 形式で指定してください（例: `2026-07-25 18:30`）。",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        st = get_store()
        channel_id = str(interaction.channel_id)
        try:
            binding = await run_sync(st.get_channel_binding, channel_id)
            country = _resolve_country(binding, self.country.value or "")
            currency = _resolve_currency(
                country,
                binding,
                self.currency.value or "",
            )
            amount_jpy, rate = await run_sync(fx.convert_to_jpy, amount_local, currency)
            memo_id = await run_sync(
                st.add_memo,
                user_id=str(interaction.user.id),
                country=country,
                amount_local=amount_local,
                currency=currency,
                rate=rate,
                amount_jpy=amount_jpy,
                memo=(self.memo.value or "").strip(),
                recorded_at=recorded_at,
                include_in_sum=False,
            )
        except Exception as e:
            log.exception("memo submit failed")
            await interaction.followup.send(
                f"メモの保存に失敗しました: {e}",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"📝 買い物メモを保存しました (ID: {memo_id})",
            color=discord.Color.teal(),
        )
        embed.add_field(name="日時", value=recorded_at, inline=True)
        embed.add_field(name="国", value=country, inline=True)
        if currency != "JPY":
            amount_text = (
                f"{_format_amount(amount_local)} {currency} → "
                f"約{_format_amount(amount_jpy)}円"
            )
        else:
            amount_text = f"{_format_amount(amount_jpy)}円"
        embed.add_field(name="金額", value=amount_text, inline=False)
        note = (self.memo.value or "").strip()
        if note:
            embed.add_field(name="メモ", value=note, inline=False)
        embed.add_field(name="集計対象", value="OFF（デフォルト）", inline=True)
        embed.set_footer(text="共有ログには入りません。下で集計対象を切り替えられます。")
        await interaction.followup.send(embed=embed, ephemeral=True)
        await interaction.followup.send(
            content=(
                f"ID {memo_id}: 集計に入れたい場合は「集計対象」を ON にしてから確定してください。"
            ),
            view=MemoOptionsView(memo_id=memo_id, owner_id=str(interaction.user.id)),
            ephemeral=True,
        )


class MemoOptionsView(discord.ui.View):
    """買い物メモの集計対象を選んで確定する。"""

    def __init__(self, memo_id: int, owner_id: str):
        super().__init__(timeout=300)
        self.memo_id = memo_id
        self.owner_id = owner_id
        self.include_in_sum = False
        self._build_items()

    def _build_items(self):
        self.clear_items()
        btn_toggle = discord.ui.Button(
            label=f"集計対象: {'ON' if self.include_in_sum else 'OFF'}",
            style=(
                discord.ButtonStyle.success
                if self.include_in_sum
                else discord.ButtonStyle.danger
            ),
            custom_id="memo_toggle_include_sum",
            row=0,
        )
        btn_toggle.callback = self.toggle_include_sum
        self.add_item(btn_toggle)

        btn_confirm = discord.ui.Button(
            label="確定",
            style=discord.ButtonStyle.primary,
            custom_id="memo_confirm",
            row=0,
        )
        btn_confirm.callback = self.confirm
        self.add_item(btn_confirm)

    async def _ensure_owner(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.owner_id:
            await interaction.response.send_message(
                "このメモの操作は本人だけできます。", ephemeral=True
            )
            return False
        return True

    async def toggle_include_sum(self, interaction: discord.Interaction):
        if not await self._ensure_owner(interaction):
            return
        self.include_in_sum = not self.include_in_sum
        self._build_items()
        await interaction.response.edit_message(view=self)

    async def confirm(self, interaction: discord.Interaction):
        if not await self._ensure_owner(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        include_in_sum = self.include_in_sum
        ok = await run_sync(
            get_store().update_memo_field,
            self.memo_id,
            "集計対象",
            "ON" if include_in_sum else "OFF",
        )
        if not ok:
            await interaction.followup.send("メモの更新に失敗しました。", ephemeral=True)
            return
        self.stop()
        status = "ON" if include_in_sum else "OFF"
        await interaction.edit_original_response(
            content=f"✅ メモ ID {self.memo_id} を確定しました（集計対象: {status}）。",
            view=None,
        )


class EntryOptionsView(discord.ui.View):
    """タグ・支払者・参加者を1メッセージ内で選び、ボタンで確定する。"""

    def __init__(
        self,
        entry_id: int,
        entry_type: str,
        tags: list[str],
        default_participant_ids: list[str] | None = None,
    ):
        super().__init__(timeout=300)
        self.entry_id = entry_id
        self.entry_type = entry_type
        self.available_tags = tags[:25]
        self.selected_tags: list[str] = []
        self.payer_id: str | None = None
        self.participant_ids: set[str] = (
            set(default_participant_ids or []) if entry_type == "支出" else set()
        )
        self.include_in_sum = False
        self._build_items()

    def _build_items(self):
        """選択状態を default に反映しつつコンポーネントを組み立てる。"""
        self.clear_items()
        next_row = 0

        if self.available_tags:
            options = [
                discord.SelectOption(
                    label=tag[:100],
                    value=tag[:100],
                    default=tag in self.selected_tags,
                )
                for tag in self.available_tags
            ]
            tag_select = discord.ui.Select(
                custom_id="entry_tags",
                placeholder="タグ（任意・複数可）",
                min_values=0,
                max_values=min(len(options), 25),
                options=options,
                row=next_row,
            )
            tag_select.callback = self.on_tags
            self.add_item(tag_select)
            next_row += 1

        if self.entry_type == "支出":
            payer_defaults = []
            if self.payer_id:
                payer_defaults = [
                    discord.SelectDefaultValue(
                        id=int(self.payer_id),
                        type=discord.SelectDefaultValueType.user,
                    )
                ]
            payer_select = discord.ui.UserSelect(
                custom_id="entry_payer",
                placeholder="支払者（割り勘時は必須）",
                min_values=0,
                max_values=1,
                row=next_row,
                default_values=payer_defaults,
            )
            payer_select.callback = self.on_payer
            self.add_item(payer_select)
            next_row += 1

            participant_defaults = [
                discord.SelectDefaultValue(
                    id=int(uid),
                    type=discord.SelectDefaultValueType.user,
                )
                for uid in sorted(self.participant_ids)
            ]
            participants_select = discord.ui.UserSelect(
                custom_id="entry_participants",
                placeholder="参加者（割り勘時・複数可）",
                min_values=0,
                max_values=10,
                row=next_row,
                default_values=participant_defaults[:25],
            )
            participants_select.callback = self.on_participants
            self.add_item(participants_select)
            next_row += 1

        button_row = next_row

        btn_toggle = discord.ui.Button(
            label=f"集計対象: {'ON' if self.include_in_sum else 'OFF'}",
            style=(
                discord.ButtonStyle.success
                if self.include_in_sum
                else discord.ButtonStyle.danger
            ),
            custom_id="toggle_include_sum",
            row=button_row,
        )
        btn_toggle.callback = self.toggle_include_sum
        self.add_item(btn_toggle)
        self._btn_toggle = btn_toggle

        if self.entry_type == "支出":
            btn_split = discord.ui.Button(
                label="割り勘ありで確定",
                style=discord.ButtonStyle.primary,
                custom_id="confirm_split",
                row=button_row,
            )
            btn_split.callback = self.confirm_split
            self.add_item(btn_split)

            btn_plain = discord.ui.Button(
                label="割り勘なしで確定",
                style=discord.ButtonStyle.secondary,
                custom_id="confirm_plain",
                row=button_row,
            )
            btn_plain.callback = self.confirm_plain
            self.add_item(btn_plain)
        else:
            btn_ok = discord.ui.Button(
                label="確定",
                style=discord.ButtonStyle.primary,
                custom_id="confirm_income",
                row=button_row,
            )
            btn_ok.callback = self.confirm_plain
            self.add_item(btn_ok)

    def _sync_selections(self):
        """現在のコンポーネント値をインスタンスに保存する。"""
        for item in self.children:
            if isinstance(item, discord.ui.Select) and item.custom_id == "entry_tags":
                if item.values:
                    self.selected_tags = list(item.values)
            elif isinstance(item, discord.ui.UserSelect) and item.custom_id == "entry_payer":
                if item.values:
                    payer = item.values[0]
                    self.payer_id = None if payer.bot else str(payer.id)
            elif isinstance(item, discord.ui.UserSelect) and item.custom_id == "entry_participants":
                if item.values:
                    self.participant_ids = {
                        str(user.id) for user in item.values if not user.bot
                    }

    async def on_tags(self, interaction: discord.Interaction):
        for item in self.children:
            if isinstance(item, discord.ui.Select) and item.custom_id == "entry_tags":
                self.selected_tags = list(item.values)
        await interaction.response.defer()

    async def on_payer(self, interaction: discord.Interaction):
        bot_selected = False
        for item in self.children:
            if isinstance(item, discord.ui.UserSelect) and item.custom_id == "entry_payer":
                payer = item.values[0] if item.values else None
                bot_selected = bool(payer and payer.bot)
                self.payer_id = str(payer.id) if payer and not payer.bot else None
        if bot_selected:
            self._build_items()
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(
                "Botは支払者に選択できません。", ephemeral=True
            )
        else:
            await interaction.response.defer()

    async def on_participants(self, interaction: discord.Interaction):
        bot_selected = False
        for item in self.children:
            if isinstance(item, discord.ui.UserSelect) and item.custom_id == "entry_participants":
                bot_selected = any(user.bot for user in item.values)
                self.participant_ids = {
                    str(user.id) for user in item.values if not user.bot
                }
        if bot_selected:
            self._build_items()
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(
                "Botは割り勘参加者に選択できません。", ephemeral=True
            )
        else:
            await interaction.response.defer()

    async def toggle_include_sum(self, interaction: discord.Interaction):
        self._sync_selections()
        self.include_in_sum = not self.include_in_sum
        self._build_items()
        await interaction.response.edit_message(view=self)

    def _collect_options(self) -> tuple[str, str | None, set[str], bool]:
        """確定時点の選択値を返す（保存済み状態を優先）。"""
        self._sync_selections()
        return (
            ",".join(self.selected_tags),
            self.payer_id,
            set(self.participant_ids),
            self.include_in_sum,
        )
    async def _finish_message(self, interaction: discord.Interaction, content: str):
        if interaction.message:
            await interaction.message.edit(content=content, view=None)
        else:
            await interaction.followup.send(content)

    async def confirm_plain(self, interaction: discord.Interaction):
        await interaction.response.defer()
        tags, _, _, include_in_sum = self._collect_options()
        st = get_store()
        await run_sync(st.update_field, self.entry_id, "タグ", tags)
        await run_sync(st.update_field, self.entry_id, "割り勘", "OFF")
        await run_sync(st.update_field, self.entry_id, "支払者ID", "")
        await run_sync(st.update_split_participants, self.entry_id, "")
        await run_sync(
            st.update_field, self.entry_id, "集計対象", "ON" if include_in_sum else "OFF"
        )

        parts = []
        if tags:
            parts.append(f"タグ: {tags}")
        if include_in_sum:
            parts.append("集計対象: ON")
        note = f"（{' / '.join(parts)}）" if parts else "（タグなし）"
        await self._finish_message(
            interaction,
            f"✅ ID {self.entry_id} の記録が完了しました{note}",
        )

    async def confirm_split(self, interaction: discord.Interaction):
        tags, payer_id, participant_ids, include_in_sum = self._collect_options()

        if not payer_id:
            await interaction.response.send_message(
                "割り勘ありの場合は、先に「支払者」を選んでから確定してください。",
                ephemeral=True,
            )
            return

        participants = set(participant_ids)
        participants.add(payer_id)
        if len(participants) < 2:
            await interaction.response.send_message(
                "割り勘ありの場合は、支払者以外の参加者も1人以上選んでください。",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        st = get_store()
        participants_str = ",".join(sorted(participants))
        await run_sync(st.update_field, self.entry_id, "タグ", tags)
        await run_sync(st.update_field, self.entry_id, "割り勘", "ON")
        await run_sync(st.update_field, self.entry_id, "支払者ID", payer_id)
        await run_sync(st.update_split_participants, self.entry_id, participants_str)
        await run_sync(
            st.update_field, self.entry_id, "集計対象", "ON" if include_in_sum else "OFF"
        )

        names = ", ".join(f"<@{uid}>" for uid in sorted(participants))
        tag_note = f"タグ: {tags}\n" if tags else ""
        agg_note = "集計対象: ON\n" if include_in_sum else ""
        await self._finish_message(
            interaction,
            (
                f"✅ ID {self.entry_id} の割り勘記録が完了しました\n"
                f"{tag_note}"
                f"{agg_note}"
                f"支払者: <@{payer_id}>\n"
                f"参加者: {names}"
            ),
        )


# ---------------------------------------------------------------------------
# スラッシュコマンド
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    try:
        await run_sync(get_store)  # Sheets接続を事前に温めておく
        # グローバル同期は反映まで最大約1時間かかることがあるため、
        # 参加中サーバーへも即時同期する
        for guild in bot.guilds:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            log.info(f"synced {len(synced)} commands to guild {guild.name} ({guild.id})")
        synced = await bot.tree.sync()
        log.info(f"synced {len(synced)} global commands")
    except Exception as e:
        log.exception(e)
    log.info(f"Logged in as {bot.user}")


@bot.tree.command(name="log_ex", description="支出を記録します")
async def log_ex(interaction: discord.Interaction):
    country_default = await _prefetch_country_default(interaction.channel_id)
    await interaction.response.send_modal(
        EntryModal(ENTRY_TYPE_EXPENSE, country_default=country_default)
    )


@bot.tree.command(name="log_in", description="予算を記録します")
async def log_in(interaction: discord.Interaction):
    country_default = await _prefetch_country_default(interaction.channel_id)
    await interaction.response.send_modal(
        EntryModal(ENTRY_TYPE_BUDGET, country_default=country_default)
    )


@bot.tree.command(name="log_ct", description="このスレッド/チャンネルを国/地域に紐付けます")
@app_commands.describe(country="紐付ける国/地域名（例: タイ）")
async def log_ct(interaction: discord.Interaction, country: str):
    await interaction.response.defer()
    country_name = country.strip()
    currency = countries.get_currency_for_country(country_name) or ""
    # スレッド単位で保存（親チャンネルには影響しない）
    await run_sync(
        get_store().bind_channel_country,
        interaction.channel_id,
        country_name,
        currency,
    )
    message = (
        f"✅ このスレッド/チャンネルを「{country_name}」に固定しました。"
        "以後この場所での /log_ex や /log_in で国は自動設定されます。"
    )
    if currency:
        message += f"\n通貨: **{currency}**（通貨欄が空欄のとき自動適用）"
    else:
        message += (
            "\n⚠️ 国名から通貨を特定できませんでした。"
            "記録時に通貨を手入力するか、国名を見直してください。"
        )
    await interaction.followup.send(message)


@bot.tree.command(name="log_tag_add", description="記録用タグを登録します")
@app_commands.describe(tag="登録するタグ名（例: 飯）")
async def log_tag_add(interaction: discord.Interaction, tag: str):
    await interaction.response.defer()
    ok = await run_sync(get_store().add_tag, tag)
    if ok:
        await interaction.followup.send(f"✅ タグ「{tag.strip()}」を登録しました。")
    else:
        await interaction.followup.send(
            f"タグ「{tag.strip()}」は既に登録済みか、空です。", ephemeral=True
        )


@bot.tree.command(name="log_tag_rm", description="登録済みタグを削除します")
@app_commands.describe(tag="削除するタグ名")
async def log_tag_rm(interaction: discord.Interaction, tag: str):
    await interaction.response.defer()
    ok = await run_sync(get_store().remove_tag, tag)
    if ok:
        await interaction.followup.send(f"🗑️ タグ「{tag.strip()}」を削除しました。")
    else:
        await interaction.followup.send(
            f"タグ「{tag.strip()}」は見つかりませんでした。", ephemeral=True
        )


@bot.tree.command(name="log_tag_ls", description="登録済みタグ一覧を表示します")
async def log_tag_ls(interaction: discord.Interaction):
    await interaction.response.defer()
    tags = await run_sync(get_store().list_tags)
    if not tags:
        await interaction.followup.send(
            "登録タグがありません。`/log_tag_add` で追加してください。"
        )
        return
    await interaction.followup.send("🏷 登録タグ: " + ", ".join(f"`{t}`" for t in tags))


@bot.tree.command(name="log_memo", description="自分用の買い物メモを記録します（共有ログには入りません）")
async def log_memo(interaction: discord.Interaction):
    country_default = await _prefetch_country_default(interaction.channel_id)
    await interaction.response.send_modal(MemoModal(country_default=country_default))


@bot.tree.command(name="log_memo_ls", description="自分の買い物メモ一覧を表示します（自分だけに表示）")
@app_commands.describe(country="絞り込む国（省略可）")
async def log_memo_ls(interaction: discord.Interaction, country: str | None = None):
    await interaction.response.defer(ephemeral=True)
    memos = await run_sync(get_store().get_memos_for_user, str(interaction.user.id))
    filtered = [m for m in memos if _match_memo(m, country)]
    if not filtered:
        await interaction.followup.send("買い物メモがありません。", ephemeral=True)
        return

    lines = []
    for m in filtered[-30:]:
        note = str(m.get("メモ") or "").strip()
        cur = str(m.get("通貨") or "JPY").upper()
        local = m.get("現地金額", "")
        jpy = float(m.get("円換算額") or 0)
        if cur != "JPY":
            amount = f"{_format_amount(local)} {cur}（{_format_amount(jpy)}円）"
        else:
            amount = f"{_format_amount(jpy)}円"
        line = (
            f"**[{m['ID']}]** {m.get('日時','')} {amount} {m.get('国','')}"
            f" [集計:{str(m.get('集計対象') or 'OFF').upper()}]"
        )
        if note:
            line += f" {note}"
        lines.append(line)

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[-4000:]
    total_all = _memo_jpy_total(filtered)
    total_on = _memo_jpy_total(filtered, require_include=True)
    embed = discord.Embed(
        title="📝 自分の買い物メモ",
        description=text,
        color=discord.Color.teal(),
    )
    embed.add_field(name="合計（全件）", value=f"{total_all:,.0f}円", inline=True)
    embed.add_field(name="合計（集計対象ON）", value=f"{total_on:,.0f}円", inline=True)
    embed.add_field(name="件数", value=f"{len(filtered)}件", inline=True)
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="log_memo_rm", description="自分の買い物メモを削除します")
@app_commands.describe(memo_ids="削除するメモID（カンマ区切り可）")
async def log_memo_rm(interaction: discord.Interaction, memo_ids: str):
    await interaction.response.defer(ephemeral=True)
    raw_ids = [t.strip() for t in re.split(r"[\s,、]+", memo_ids) if t.strip()]
    if not raw_ids:
        await interaction.followup.send("削除するメモIDを指定してください。", ephemeral=True)
        return

    st = get_store()
    user_id = str(interaction.user.id)
    deleted = []
    skipped = []
    for token in raw_ids:
        if not token.isdigit():
            skipped.append(token)
            continue
        memo_id = int(token)
        memo = await run_sync(st.get_memo_by_id, memo_id)
        if memo is None:
            skipped.append(token)
            continue
        if str(memo.get("ユーザーID")) != user_id:
            skipped.append(token)
            continue
        ok = await run_sync(st.delete_memo, memo_id)
        if ok:
            deleted.append(str(memo_id))
        else:
            skipped.append(token)

    parts = []
    if deleted:
        parts.append(f"🗑️ 削除しました: ID {', '.join(deleted)}")
    if skipped:
        parts.append(f"スキップ: {', '.join(skipped)}（存在しない／他人のメモ／不正なID）")
    await interaction.followup.send("\n".join(parts) or "削除対象がありませんでした。", ephemeral=True)


def _split_summary(entry: dict, *, name_of=None) -> str:
    """割り勘対象の記録なら、支払者・参加者を短い文字列にして返す（対象外なら空文字）。

    name_of を渡すとメンションではなく表示名で出す（log_sum / log_ls 用）。
    省略時は <@ID> メンション（log_wari など向け）。
    """
    if entry.get("割り勘") != "ON":
        return ""
    payer = _valid_discord_id(entry.get("支払者ID", ""))
    participants = _participant_ids(entry)

    def label(uid: str) -> str:
        if name_of:
            return name_of(uid)
        return f"<@{uid}>"

    if not payer:
        return "  ⚠️割り勘ONですが支払者未設定"
    if not participants:
        return f"  ⚠️参加者IDが未設定または破損（支払者:{label(payer)}）"
    others = [p for p in participants if p != payer]
    others_str = " ".join(label(p) for p in others) if others else "なし"
    return f"  💰割り勘 支払者:{label(payer)} 参加者:{others_str}"


async def _build_name_resolver(guild: discord.Guild | None, user_ids: set[str]):
    """ユーザーID → 表示名の辞書を作る。メンションは使わない。"""
    names: dict[str, str] = {}
    for uid in user_ids:
        if not uid or uid in names:
            continue
        member = guild.get_member(int(uid)) if guild else None
        if member is not None:
            names[uid] = member.display_name
            continue
        try:
            user = await bot.fetch_user(int(uid))
            names[uid] = user.global_name or user.name
        except Exception:
            names[uid] = f"不明({uid[-4:]})"
    return lambda uid: names.get(uid, f"不明({uid[-4:]})")


def _collect_split_user_ids(entries: list[dict]) -> set[str]:
    ids: set[str] = set()
    for e in entries:
        if e.get("割り勘") != "ON":
            continue
        payer = _valid_discord_id(e.get("支払者ID", ""))
        if payer:
            ids.add(payer)
        ids.update(_participant_ids(e))
    return ids


def _match_entry(entry: dict, country: str | None, tag: str | None) -> bool:
    if country and entry.get("国") != country:
        return False
    if tag:
        entry_tags = _entry_tag_list(entry)
        if tag not in entry_tags:
            return False
    return True


def _entry_tag_list(entry: dict) -> list[str]:
    """タグ列を分割（カンマ・読点・空白区切りに対応）。"""
    raw = unicodedata.normalize("NFKC", str(entry.get("タグ", "") or ""))
    return [p.strip() for p in re.split(r"[,、/\s|]+", raw) if p.strip()]


def _entry_has_tag(entry: dict, tag: str) -> bool:
    target = unicodedata.normalize("NFKC", str(tag)).strip()
    if not target:
        return False
    return any(
        unicodedata.normalize("NFKC", t).strip() == target
        for t in _entry_tag_list(entry)
    )


def _should_exclude_exchange_tag(country: str | None, tag: str | None) -> bool:
    """国指定かつ tag=両替 の明示指定でないとき、両替タグを除外する。"""
    if not country:
        return False
    explicit = unicodedata.normalize("NFKC", str(tag or "")).strip()
    return explicit != SUM_COUNTRY_EXCLUDE_TAG


def _match_memo(memo: dict, country: str | None) -> bool:
    if country and str(memo.get("国", "")).strip() != country:
        return False
    return True


def _is_memo_included_in_sum(memo: dict) -> bool:
    return str(memo.get("集計対象", "OFF")).strip().upper() == "ON"


def _memo_jpy_total(
    memos: list[dict],
    country: str | None = None,
    *,
    require_include: bool = False,
) -> float:
    total = 0.0
    for memo in memos:
        if not _match_memo(memo, country):
            continue
        if require_include and not _is_memo_included_in_sum(memo):
            continue
        try:
            total += float(memo.get("円換算額") or 0)
        except (TypeError, ValueError):
            continue
    return total


def _memo_totals_by_user(
    memos: list[dict],
    *,
    require_include: bool = False,
) -> dict[str, float]:
    by_user: dict[str, float] = defaultdict(float)
    for memo in memos:
        if require_include and not _is_memo_included_in_sum(memo):
            continue
        uid = _valid_discord_id(memo.get("ユーザーID", ""))
        if not uid:
            continue
        try:
            by_user[uid] += float(memo.get("円換算額") or 0)
        except (TypeError, ValueError):
            continue
    return dict(by_user)


def _is_expense_entry(entry: dict) -> bool:
    return str(entry.get("種別", "")).strip() == ENTRY_TYPE_EXPENSE


def _is_budget_entry(entry: dict) -> bool:
    """予算（旧「収入」含む）。"""
    return str(entry.get("種別", "")).strip() in ENTRY_TYPE_BUDGET_ALIASES


def _display_entry_type(entry_type: str) -> str:
    """表示用。旧データの「収入」も「予算」と出す。"""
    text = str(entry_type or "").strip()
    if text in ENTRY_TYPE_BUDGET_ALIASES:
        return ENTRY_TYPE_BUDGET
    return text or "-"


def _entry_sign(entry: dict) -> str:
    return "-" if _is_expense_entry(entry) else "+"


def _is_included_in_sum(entry: dict) -> bool:
    return str(entry.get("集計対象", "OFF")).strip().upper() == "ON"


def _currency_totals(entries: list[dict], entry_type: str | None = None) -> dict[str, dict[str, float]]:
    by_currency: dict[str, dict[str, float]] = defaultdict(lambda: {"local": 0.0, "jpy": 0.0})
    for e in entries:
        if entry_type and e.get("種別") != entry_type:
            continue
        cur = str(e.get("通貨") or "JPY").upper()
        try:
            local = float(e.get("現地金額") or 0)
            jpy = float(e.get("円換算額") or 0)
        except (TypeError, ValueError):
            continue
        by_currency[cur]["local"] += local
        by_currency[cur]["jpy"] += jpy
    return dict(by_currency)


def _format_currency_breakdown(by_currency: dict[str, dict[str, float]]) -> str:
    if not by_currency:
        return "なし"
    lines = []
    for cur in sorted(by_currency.keys()):
        local = by_currency[cur]["local"]
        jpy = by_currency[cur]["jpy"]
        if cur == "JPY":
            lines.append(f"**{cur}**: {_format_amount(local)}円")
        else:
            lines.append(
                f"**{cur}**: {_format_amount(local)} (約{_format_amount(jpy)}円)"
            )
    return "\n".join(lines)


@bot.tree.command(name="log_sum", description="国・タグで絞り込んで集計します（自分だけに表示）")
@app_commands.describe(country="絞り込む国（省略可）", tag="絞り込むタグ（省略可）")
async def log_sum(interaction: discord.Interaction, country: str | None = None, tag: str | None = None):
    if not await _safe_defer(interaction, ephemeral=True):
        return
    entries = await sheets("get_all_entries")
    has_filter = bool(country or tag)
    filtered = [
        e for e in entries
        if _match_entry(e, country, tag)
        and (has_filter or _is_included_in_sum(e))
    ]
    # 国指定時は「両替」タグを除外（ただし tag=両替 で明示指定したときは残す）
    excluded_exchange = 0
    if _should_exclude_exchange_tag(country, tag):
        kept = []
        for e in filtered:
            if _entry_has_tag(e, SUM_COUNTRY_EXCLUDE_TAG):
                excluded_exchange += 1
                continue
            kept.append(e)
        filtered = kept
        log.info(
            "log_sum country=%r excluded tag=%r count=%s remaining=%s",
            country,
            SUM_COUNTRY_EXCLUDE_TAG,
            excluded_exchange,
            len(filtered),
        )

    my_memos = await sheets("get_memos_for_user", str(interaction.user.id))
    # タグ絞り込み時はメモにタグが無いので加算しない。
    # 国指定時は同じ国のメモ（集計対象OFF含む）。未指定時は集計対象ONのみ。
    if tag:
        memo_total = 0.0
        memo_note = "タグ指定時は買い物メモを加算しません"
    else:
        memo_total = _memo_jpy_total(
            my_memos,
            country,
            require_include=not bool(country),
        )
        memo_note = None

    if not filtered and memo_total <= 0:
        if has_filter:
            await interaction.followup.send("該当する記録がありませんでした。", ephemeral=True)
        else:
            await interaction.followup.send(
                "該当する記録がありませんでした（集計対象ONの記録のみ集計します）。",
                ephemeral=True,
            )
        return

    expense_entries = [e for e in filtered if _is_expense_entry(e)]
    budget_entries = [e for e in filtered if _is_budget_entry(e)]
    total_expense = sum(e["円換算額"] for e in expense_entries)
    total_budget = sum(e["円換算額"] for e in budget_entries)
    balance = total_budget - total_expense
    expense_with_memo = total_expense + memo_total

    title_parts = []
    if country:
        title_parts.append(country)
    if tag:
        title_parts.append(f"#{tag}")
    title = " / ".join(title_parts) if title_parts else "全期間"

    embed = discord.Embed(title=f"📊 集計: {title}", color=discord.Color.blue())
    embed.add_field(
        name="支出合計（円）",
        value=f"{total_expense:,.0f}円（メモ込み: {expense_with_memo:,.0f}円）",
        inline=True,
    )
    embed.add_field(name="残高（予算 − 支出）", value=f"{balance:,.0f}円", inline=True)
    embed.add_field(name="件数", value=f"{len(filtered)}件", inline=True)
    if memo_total > 0:
        embed.add_field(
            name="自分の買い物メモ",
            value=f"{memo_total:,.0f}円",
            inline=True,
        )

    expense_by_currency = _currency_totals(expense_entries)
    if expense_by_currency:
        embed.add_field(
            name="支出（通貨別）",
            value=_format_currency_breakdown(expense_by_currency),
            inline=False,
        )

    # 直近5件だけ内訳表示（メンションせず表示名のみ）
    recent = filtered[-5:]
    name_of = await _build_name_resolver(
        interaction.guild, _collect_split_user_ids(recent)
    )
    lines = []
    for e in recent:
        sign = _entry_sign(e)
        line = f"[{e['ID']}] {sign}{e['円換算額']:,.0f}円 {e.get('タグ','')} {e.get('メモ','')}"
        line += _split_summary(e, name_of=name_of)
        lines.append(line)
    if lines:
        embed.add_field(name="直近の記録", value="\n".join(lines), inline=False)

    footer_parts = []
    if country:
        footer_parts.append("国指定は現金利用の参考値（集計対象OFF含む・公式集計は /log_all）")
    elif has_filter:
        footer_parts.append("タグ指定時は集計対象OFFの記録も含みます")
    if _should_exclude_exchange_tag(country, tag):
        footer_parts.append(
            f"タグ「{SUM_COUNTRY_EXCLUDE_TAG}」を{excluded_exchange}件除外"
        )
    if memo_note:
        footer_parts.append(memo_note)
    if footer_parts:
        embed.set_footer(text=" / ".join(footer_parts))

    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.tree.command(name="log_all", description="全体サマリー（MEMO抜き / ユーザー別メモ / 自分のメモ込）")
async def log_all(interaction: discord.Interaction):
    await interaction.response.defer()
    st = get_store()
    entries = await run_sync(st.get_all_entries)
    all_memos = await run_sync(st.get_all_memos)
    aggregated = [e for e in entries if _is_included_in_sum(e)]

    if not aggregated and not all_memos:
        await interaction.followup.send("記録がまだありません。")
        return

    total_expense = sum(e["円換算額"] for e in aggregated if _is_expense_entry(e))
    total_budget = sum(e["円換算額"] for e in aggregated if _is_budget_entry(e))

    by_country = defaultdict(float)
    for e in aggregated:
        if _is_expense_entry(e):
            by_country[e.get("国", DEFAULT_COUNTRY)] += e["円換算額"]

    my_id = str(interaction.user.id)
    my_memo_total = _memo_jpy_total(
        [m for m in all_memos if m.get("ユーザーID") == my_id],
        require_include=True,
    )
    memo_by_user = _memo_totals_by_user(all_memos, require_include=True)
    name_of = await _build_name_resolver(interaction.guild, set(memo_by_user.keys()))

    embed = discord.Embed(
        title="🌍 全体サマリー",
        color=discord.Color.purple(),
    )

    # 1) MEMO抜き
    if aggregated:
        no_memo_lines = [
            f"支出: {total_expense:,.0f}円",
            f"予算: {total_budget:,.0f}円",
            f"残高: {total_budget - total_expense:,.0f}円",
        ]
        country_lines = [
            f"{c}: {v:,.0f}円"
            for c, v in sorted(by_country.items(), key=lambda x: -x[1])
        ]
        if country_lines:
            no_memo_lines.append("国別支出:")
            no_memo_lines.extend(f"・{line}" for line in country_lines)
        embed.add_field(
            name="① MEMO抜き（共有・集計対象ON）",
            value="\n".join(no_memo_lines),
            inline=False,
        )
    else:
        embed.add_field(
            name="① MEMO抜き（共有・集計対象ON）",
            value="集計対象ONの記録なし",
            inline=False,
        )

    # 2) それぞれのMEMO合計
    if memo_by_user:
        user_lines = [
            f"{name_of(uid)}: {amount:,.0f}円"
            for uid, amount in sorted(memo_by_user.items(), key=lambda x: -x[1])
        ]
        embed.add_field(
            name="② それぞれのMEMO合計（集計対象ON）",
            value="\n".join(user_lines),
            inline=False,
        )
    else:
        embed.add_field(
            name="② それぞれのMEMO合計（集計対象ON）",
            value="集計対象ONの買い物メモなし",
            inline=False,
        )

    # 3) 自分のMEMO込（共有支出は2人割り想定で半分）
    shared_half = float(total_expense) / 2
    embed.add_field(
        name="③ 自分のMEMO込（集計対象ON）",
        value=(
            f"共有支出÷2 {shared_half:,.0f}円 + 自分のメモ {my_memo_total:,.0f}円\n"
            f"= **{shared_half + my_memo_total:,.0f}円**"
        ),
        inline=False,
    )

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="log_ls", description="ID付きで記録を一覧表示します（直近20件・自分だけに表示）")
@app_commands.describe(country="絞り込む国（省略可）")
async def log_ls(interaction: discord.Interaction, country: str | None = None):
    await interaction.response.defer(ephemeral=True)
    entries = await run_sync(get_store().get_all_entries)
    filtered = [e for e in entries if _match_entry(e, country, None)]
    filtered = filtered[-20:]

    if not filtered:
        await interaction.followup.send("該当する記録がありませんでした。", ephemeral=True)
        return

    name_of = await _build_name_resolver(
        interaction.guild, _collect_split_user_ids(filtered)
    )
    lines = []
    for e in filtered:
        sign = _entry_sign(e)
        line = (
            f"**[{e['ID']}]** {e['日時']} {sign}{e['円換算額']:,.0f}円 {e.get('国','')} "
            f"{e.get('タグ','')} {e.get('メモ','')}"
        )
        split_note = _split_summary(e, name_of=name_of)
        if split_note:
            line += "\n" + split_note
        lines.append(line)

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[-4000:]

    embed = discord.Embed(title="📋 記録一覧", description=text, color=discord.Color.blue())
    await interaction.followup.send(embed=embed, ephemeral=True)


EDIT_FIELDS = [
    ("日時", "日時"),
    ("現地金額", "現地金額"),
    ("通貨", "通貨"),
    ("タグ", "タグ"),
    ("メモ", "メモ"),
    ("国", "国"),
    ("集計対象 ON/OFF", "集計対象"),
    ("割り勘ON/OFF", "割り勘"),
    ("支払者", "支払者ID"),
    ("参加者", "参加者ID"),
]

DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
)


def _parse_mentions(value: str) -> list[str]:
    """'@user1 @user2' のようなメンション文字列からユーザーIDのリストを取り出す"""
    return MENTION_RE.findall(value)


def _parse_datetime(value: str) -> str | None:
    """入力日時を 'YYYY-MM-DD HH:MM' に正規化する。失敗時は None。"""
    text = value.strip()
    for fmt in DATETIME_FORMATS:
        try:
            return datetime.datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return None


def _mention_or_raw(user_id: str) -> str:
    uid = _valid_discord_id(user_id)
    return f"<@{uid}>" if uid else (user_id or "（未設定）")


def _entry_detail_embed(entry: dict) -> discord.Embed:
    """編集前に現状を見せる用の詳細Embed。"""
    entry_id = entry.get("ID")
    entry_type = entry.get("種別", "")
    embed = discord.Embed(
        title=f"✏️ 記録の編集 (ID: {entry_id})",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="種別", value=_display_entry_type(entry_type), inline=True)
    embed.add_field(name="日時", value=str(entry.get("日時") or "-"), inline=True)
    embed.add_field(name="国", value=str(entry.get("国") or "-"), inline=True)

    currency = str(entry.get("通貨") or "JPY")
    amount_local = entry.get("現地金額", "")
    amount_jpy = entry.get("円換算額", "")
    if currency != "JPY":
        amount_text = (
            f"{_format_amount(amount_local)} {currency} → {_format_amount(amount_jpy)}円"
        )
    else:
        amount_text = f"{_format_amount(amount_jpy)}円"
    embed.add_field(name="金額", value=amount_text, inline=False)
    embed.add_field(name="タグ", value=str(entry.get("タグ") or "（なし）"), inline=True)
    embed.add_field(name="メモ", value=str(entry.get("メモ") or "（なし）"), inline=True)
    embed.add_field(name="集計対象", value=str(entry.get("集計対象") or "OFF"), inline=True)
    embed.add_field(name="割り勘", value=str(entry.get("割り勘") or "OFF"), inline=True)

    participants = _participant_ids(entry)
    embed.add_field(
        name="支払者",
        value=_mention_or_raw(str(entry.get("支払者ID", ""))),
        inline=True,
    )
    if participants:
        embed.add_field(
            name="参加者",
            value=" ".join(f"<@{p}>" for p in participants),
            inline=False,
        )
    else:
        embed.add_field(name="参加者", value="（未設定）", inline=False)

    embed.set_footer(text="下のメニューから修正する項目を選んでください")
    return embed


async def _apply_field_update(
    interaction: discord.Interaction,
    entry_id: int,
    field: str,
    value: str,
    target: dict,
) -> bool:
    """
    項目更新を行い、成功時は True。失敗時は ephemeral で理由を返して False。
    interaction はすでに defer 済みでも、未応答でもよい（内部で分岐）。
    """
    st = get_store()

    async def reply(msg: str, *, ephemeral: bool = False):
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(msg, ephemeral=ephemeral)

    if field == "日時":
        normalized = _parse_datetime(value)
        if not normalized:
            await reply(
                "日時は `YYYY-MM-DD HH:MM` 形式で指定してください（例: `2026-07-25 18:30`）。",
                ephemeral=True,
            )
            return False
        await run_sync(st.update_field, entry_id, "日時", normalized)
        await reply(f"✅ ID {entry_id} の日時を {normalized} に更新しました。")
        return True

    if field in ("現地金額", "通貨"):
        try:
            if field == "現地金額":
                amount_local = _parse_amount(value)
            else:
                amount_local = _parse_amount(str(target["現地金額"]))
        except ValueError:
            await reply("金額は数字で入力してください（小数点可）。", ephemeral=True)
            return False
        currency = value.upper() if field == "通貨" else str(target["通貨"])
        try:
            amount_jpy, rate = await run_sync(fx.convert_to_jpy, amount_local, currency)
        except Exception as e:
            await reply(f"為替レート取得に失敗しました: {e}", ephemeral=True)
            return False
        await run_sync(st.update_field, entry_id, "現地金額", str(amount_local))
        await run_sync(st.update_field, entry_id, "通貨", currency)
        await run_sync(st.update_field, entry_id, "レート", str(rate))
        await run_sync(st.update_field, entry_id, "円換算額", str(amount_jpy))
        await reply(
            f"✅ ID {entry_id} を更新しました: "
            f"{_format_amount(amount_local)} {currency} → {_format_amount(amount_jpy)}円"
        )
        return True

    if field == "割り勘":
        normalized = value.strip().upper()
        if normalized not in ("ON", "OFF"):
            await reply("割り勘の値は ON か OFF で指定してください。", ephemeral=True)
            return False
        await run_sync(st.update_field, entry_id, "割り勘", normalized)
        await reply(f"✅ ID {entry_id} の割り勘を {normalized} にしました。")
        return True

    if field == "集計対象":
        normalized = value.strip().upper()
        if normalized not in ("ON", "OFF"):
            await reply("集計対象の値は ON か OFF で指定してください。", ephemeral=True)
            return False
        await run_sync(st.update_field, entry_id, "集計対象", normalized)
        await reply(f"✅ ID {entry_id} の集計対象を {normalized} にしました。")
        return True

    if field == "支払者ID":
        ids = _parse_mentions(value) or ([value] if _valid_discord_id(value) else [])
        if not ids:
            await reply("支払者は @ユーザー名 で指定してください。", ephemeral=True)
            return False
        await run_sync(st.update_field, entry_id, "支払者ID", ids[0])
        await reply(f"✅ ID {entry_id} の支払者を <@{ids[0]}> にしました。")
        return True

    if field == "参加者ID":
        ids = _parse_mentions(value)
        if not ids:
            # カンマ区切りの生IDも許容
            ids = [p for p in re.split(r"[\s,、]+", value) if _valid_discord_id(p)]
        if not ids:
            await reply("参加者は @ユーザー名 で指定してください（複数可）。", ephemeral=True)
            return False
        payer = _valid_discord_id(target.get("支払者ID", ""))
        if payer:
            ids.append(payer)
        ids = sorted(set(ids))
        await run_sync(st.update_field, entry_id, "参加者ID", ",".join(ids))
        names = ", ".join(f"<@{uid}>" for uid in ids)
        await reply(f"✅ ID {entry_id} の参加者を更新しました: {names}")
        return True

    ok = await run_sync(st.update_field, entry_id, field, value)
    if ok:
        await reply(f"✅ ID {entry_id} の「{field}」を更新しました: {value}")
        return True
    await reply(f"ID {entry_id} が見つかりませんでした。", ephemeral=True)
    return False


class EditValueModal(discord.ui.Modal):
    def __init__(self, entry_id: int, field: str, current: str, target: dict):
        labels = {
            "日時": "日時 (YYYY-MM-DD HH:MM)",
            "現地金額": "現地金額",
            "通貨": "通貨コード",
            "タグ": "タグ（カンマ区切り）",
            "メモ": "メモ",
            "国": "国",
            "支払者ID": "支払者（@メンション）",
            "参加者ID": "参加者（@メンション 複数可）",
        }
        super().__init__(title=f"ID {entry_id}: {labels.get(field, field)} を編集")
        self.entry_id = entry_id
        self.field = field
        self.target = target

        default = str(current or "")
        if field == "支払者ID" and _valid_discord_id(default):
            default = f"<@{default}>"
        elif field == "参加者ID":
            ids = _participant_ids({"参加者ID": default})
            default = " ".join(f"<@{uid}>" for uid in ids)

        self.value_input = discord.ui.TextInput(
            label=labels.get(field, field)[:45],
            default=default[:400] if default else None,
            placeholder="新しい値を入力",
            required=True,
            max_length=200,
            style=discord.TextStyle.paragraph if field in ("メモ", "参加者ID", "タグ") else discord.TextStyle.short,
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        # 最新の支払者情報などが必要な場合に備え、再度読む
        entries = await run_sync(get_store().get_all_entries)
        target = next((e for e in entries if str(e["ID"]) == str(self.entry_id)), self.target)
        await _apply_field_update(
            interaction, self.entry_id, self.field, self.value_input.value, target
        )


class EditFieldView(discord.ui.View):
    def __init__(self, entry_id: int, target: dict):
        super().__init__(timeout=300)
        self.entry_id = entry_id
        self.target = target

        select = discord.ui.Select(
            placeholder="修正する項目を選択",
            options=[
                discord.SelectOption(label=label, value=value)
                for label, value in EDIT_FIELDS
            ],
        )
        select.callback = self.on_field
        self.add_item(select)

    async def on_field(self, interaction: discord.Interaction):
        field = None
        for item in self.children:
            if isinstance(item, discord.ui.Select):
                field = item.values[0]
        if not field:
            await interaction.response.send_message("項目を選択してください。", ephemeral=True)
            return

        if field == "割り勘":
            current = str(self.target.get("割り勘") or "OFF")
            await interaction.response.edit_message(
                content=f"ID {self.entry_id}: 割り勘を選んでください（現在: {current}）",
                view=EditSplitView(self.entry_id, self.target),
            )
            return

        if field == "集計対象":
            current = str(self.target.get("集計対象") or "OFF")
            await interaction.response.edit_message(
                content=f"ID {self.entry_id}: 集計対象を選んでください（現在: {current}）",
                view=EditIncludeSumView(self.entry_id, self.target),
            )
            return

        if field == "支払者ID":
            current = _mention_or_raw(str(self.target.get("支払者ID", "")))
            await interaction.response.edit_message(
                content=f"ID {self.entry_id}: 支払者を選んでください（現在: {current}）",
                view=EditPayerView(self.entry_id, self.target),
            )
            return

        if field == "参加者ID":
            participants = _participant_ids(self.target)
            current = " ".join(f"<@{p}>" for p in participants) if participants else "（未設定）"
            await interaction.response.edit_message(
                content=f"ID {self.entry_id}: 参加者を選んでください（現在: {current}）",
                view=EditParticipantsView(self.entry_id, self.target),
            )
            return

        current_map = {
            "日時": self.target.get("日時", ""),
            "現地金額": self.target.get("現地金額", ""),
            "通貨": self.target.get("通貨", ""),
            "タグ": self.target.get("タグ", ""),
            "メモ": self.target.get("メモ", ""),
            "国": self.target.get("国", ""),
        }
        await interaction.response.send_modal(
            EditValueModal(
                self.entry_id,
                field,
                str(current_map.get(field, "")),
                self.target,
            )
        )


class EditSplitView(discord.ui.View):
    def __init__(self, entry_id: int, target: dict):
        super().__init__(timeout=180)
        self.entry_id = entry_id
        self.target = target

    @discord.ui.button(label="ON", style=discord.ButtonStyle.primary)
    async def set_on(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await _apply_field_update(interaction, self.entry_id, "割り勘", "ON", self.target)
        await interaction.edit_original_response(view=None)

    @discord.ui.button(label="OFF", style=discord.ButtonStyle.secondary)
    async def set_off(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await _apply_field_update(interaction, self.entry_id, "割り勘", "OFF", self.target)
        await interaction.edit_original_response(view=None)


class EditIncludeSumView(discord.ui.View):
    def __init__(self, entry_id: int, target: dict):
        super().__init__(timeout=180)
        self.entry_id = entry_id
        self.target = target

    @discord.ui.button(label="ON", style=discord.ButtonStyle.success)
    async def set_on(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await _apply_field_update(interaction, self.entry_id, "集計対象", "ON", self.target)
        await interaction.edit_original_response(view=None)

    @discord.ui.button(label="OFF", style=discord.ButtonStyle.danger)
    async def set_off(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await _apply_field_update(interaction, self.entry_id, "集計対象", "OFF", self.target)
        await interaction.edit_original_response(view=None)


class EditPayerView(discord.ui.View):
    def __init__(self, entry_id: int, target: dict):
        super().__init__(timeout=180)
        self.entry_id = entry_id
        self.target = target
        select = discord.ui.UserSelect(placeholder="新しい支払者", min_values=1, max_values=1)
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        payer_id = None
        for item in self.children:
            if isinstance(item, discord.ui.UserSelect) and item.values:
                payer_id = str(item.values[0].id)
        if not payer_id:
            await interaction.response.send_message("支払者を選択してください。", ephemeral=True)
            return
        await interaction.response.defer()
        await _apply_field_update(interaction, self.entry_id, "支払者ID", payer_id, self.target)
        await interaction.edit_original_response(view=None)


class EditParticipantsView(discord.ui.View):
    def __init__(self, entry_id: int, target: dict):
        super().__init__(timeout=180)
        self.entry_id = entry_id
        self.target = target
        select = discord.ui.UserSelect(
            placeholder="新しい参加者（複数可）", min_values=1, max_values=10
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        ids = []
        for item in self.children:
            if isinstance(item, discord.ui.UserSelect):
                ids = [str(u.id) for u in item.values]
        if not ids:
            await interaction.response.send_message("参加者を選択してください。", ephemeral=True)
            return
        await interaction.response.defer()
        mentions = " ".join(f"<@{uid}>" for uid in ids)
        await _apply_field_update(interaction, self.entry_id, "参加者ID", mentions, self.target)
        await interaction.edit_original_response(view=None)


@bot.tree.command(name="log_ed", description="IDを指定して記録を確認・修正します")
@app_commands.describe(id="修正したい記録のID")
async def log_ed(interaction: discord.Interaction, id: int):
    await interaction.response.defer()
    entries = await run_sync(get_store().get_all_entries)
    target = next((e for e in entries if str(e["ID"]) == str(id)), None)
    if target is None:
        await interaction.followup.send(f"ID {id} が見つかりませんでした。", ephemeral=True)
        return

    await interaction.followup.send(
        content="修正する項目を選んでください。",
        embed=_entry_detail_embed(target),
        view=EditFieldView(entry_id=id, target=target),
    )


@bot.tree.command(name="log_rm", description="IDを複数指定して記録を削除します")
@app_commands.describe(ids="削除するID（例: 23,24,25 または 23 24 25）")
async def log_rm(interaction: discord.Interaction, ids: str):
    tokens = [value for value in re.split(r"[\s,、]+", ids.strip()) if value]
    if not tokens or any(not value.isdigit() for value in tokens):
        await interaction.response.send_message(
            "IDは数字をカンマまたは空白で区切ってください（例: `23,24,25`）。",
            ephemeral=True,
        )
        return

    # 入力順を保ったまま重複を除く
    entry_ids = list(dict.fromkeys(int(value) for value in tokens))
    await interaction.response.defer()
    st = get_store()
    deleted = []
    missing = []
    for entry_id in entry_ids:
        if await run_sync(st.delete_entry, entry_id):
            deleted.append(str(entry_id))
        else:
            missing.append(str(entry_id))

    messages = []
    if deleted:
        messages.append(f"🗑️ ID {', '.join(deleted)} を削除しました。")
    if missing:
        messages.append(f"⚠️ ID {', '.join(missing)} は見つかりませんでした。")
    await interaction.followup.send("\n".join(messages), ephemeral=not deleted)


@bot.tree.command(name="log_wari", description="割り勘の精算額を計算します")
@app_commands.describe(country="絞り込む国（省略可）")
async def log_wari(interaction: discord.Interaction, country: str | None = None):
    await interaction.response.defer()
    entries = await run_sync(get_store().get_all_entries)

    split_entries = [
        e for e in entries
        if e.get("割り勘") == "ON" and _is_expense_entry(e) and (not country or e.get("国") == country)
    ]

    if not split_entries:
        await interaction.followup.send("割り勘対象の記録がありませんでした。")
        return

    balance = defaultdict(float)  # user_id -> balance (プラス=貰える, マイナス=払う)
    invalid_entry_ids = []

    for e in split_entries:
        participants = _participant_ids(e)
        payer = _valid_discord_id(e.get("支払者ID", ""))
        if not participants or not payer:
            invalid_entry_ids.append(str(e.get("ID", "?")))
            continue
        amount = float(e["円換算額"])
        share = amount / len(participants)
        for p in participants:
            if p == payer:
                continue
            balance[p] -= share
            balance[payer] += share

    # 貸し借りを最小回数の送金に整理する（貪欲法）
    debtors = sorted([(uid, bal) for uid, bal in balance.items() if bal < -0.5], key=lambda x: x[1])
    creditors = sorted([(uid, bal) for uid, bal in balance.items() if bal > 0.5], key=lambda x: -x[1])

    transactions = []
    di, ci = 0, 0
    debtors = [list(x) for x in debtors]
    creditors = [list(x) for x in creditors]

    while di < len(debtors) and ci < len(creditors):
        debt_uid, debt_amt = debtors[di]
        cred_uid, cred_amt = creditors[ci]
        pay = min(-debt_amt, cred_amt)
        transactions.append((debt_uid, cred_uid, pay))
        debtors[di][1] += pay
        creditors[ci][1] -= pay
        if abs(debtors[di][1]) < 1:
            di += 1
        if abs(creditors[ci][1]) < 1:
            ci += 1

    if not transactions:
        if invalid_entry_ids:
            ids = ", ".join(invalid_entry_ids)
            await interaction.followup.send(
                f"⚠️ ID {ids} の参加者データが破損しています。"
                " `/log_ed` で参加者をメンション指定し直してください。",
                ephemeral=True,
            )
            return
        await interaction.followup.send("精算の必要はありません（すでに帳尻が合っています）。")
        return

    lines = [f"<@{a}> → <@{b}> に {amt:,.0f}円" for a, b, amt in transactions]
    title = f"💰 割り勘精算" + (f"（{country}）" if country else "")
    embed = discord.Embed(title=title, description="\n".join(lines), color=discord.Color.gold())

    split_by_currency = _currency_totals(split_entries, "支出")
    if split_by_currency:
        embed.add_field(
            name="割り勘対象（通貨別）",
            value=_format_currency_breakdown(split_by_currency),
            inline=False,
        )
    invalid_set = set(invalid_entry_ids)
    valid_entries = [e for e in split_entries if str(e.get("ID", "?")) not in invalid_set]
    total_jpy = sum(float(e["円換算額"]) for e in valid_entries)
    embed.add_field(name="割り勘対象合計（円）", value=f"{total_jpy:,.0f}円", inline=False)

    if invalid_entry_ids:
        embed.set_footer(
            text=f"参加者データ破損のため除外: ID {', '.join(invalid_entry_ids)}"
        )
    await interaction.followup.send(embed=embed)


if __name__ == "__main__":
    token = os.environ["DISCORD_BOT_TOKEN"]
    bot.run(token)
