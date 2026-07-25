"""
TabiLog Bot - 世界一周の費用ログ用Discord Bot

コマンド一覧:
  /log_ex   支出を記録（モーダル入力、割り勘ON時は参加者選択が続く）
  /log_in   収入を記録（モーダル入力）
  /log_ct   このチャンネルを国に紐付け
  /log_sum  国・タグで絞り込み集計
  /log_all  全体サマリー
  /log_ls   ID付き一覧表示
  /log_ed   IDを指定して項目を修正
  /log_rm   IDを指定して削除
  /log_wari 割り勘の精算計算
"""
import os
import re
import logging
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

import fx
from sheets import SheetStore

load_dotenv()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tabilog")

DEFAULT_COUNTRY = "不明"  # 国が紐付いていないチャンネル用のデフォルト
DISCORD_ID_RE = re.compile(r"\d{17,20}")
MENTION_RE = re.compile(r"<@!?(\d{17,20})>")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

store: SheetStore | None = None


def get_store() -> SheetStore:
    global store
    if store is None:
        store = SheetStore()
    return store


def _valid_discord_id(value) -> str | None:
    """保存値が正しいDiscordユーザーIDなら返す。壊れた数値は除外する。"""
    cleaned = str(value).strip().lstrip("'")
    return cleaned if DISCORD_ID_RE.fullmatch(cleaned) else None


def _participant_ids(entry: dict) -> list[str]:
    values = str(entry.get("参加者ID", "")).split(",")
    return [uid for value in values if (uid := _valid_discord_id(value))]


# ---------------------------------------------------------------------------
# 支出/収入 記録用モーダル
# ---------------------------------------------------------------------------

class EntryModal(discord.ui.Modal):
    def __init__(self, entry_type: str):
        super().__init__(title=f"{entry_type}を記録")
        self.entry_type = entry_type

        self.amount = discord.ui.TextInput(
            label="金額", placeholder="例: 250", required=True, max_length=20
        )
        self.currency = discord.ui.TextInput(
            label="通貨（現地通貨コード。日本円ならJPYか空欄）",
            placeholder="例: THB / USD / JPY",
            required=False,
            max_length=10,
        )
        self.tags = discord.ui.TextInput(
            label="タグ（カンマ区切りで複数可）",
            placeholder="例: 飯,屋台",
            required=False,
            max_length=100,
        )
        self.memo = discord.ui.TextInput(
            label="メモ", required=False, max_length=200
        )

        self.add_item(self.amount)
        self.add_item(self.currency)
        self.add_item(self.tags)
        self.add_item(self.memo)

        if entry_type == "支出":
            self.split = discord.ui.TextInput(
                label="割り勘（on / off）", placeholder="off", required=False, max_length=5
            )
            self.add_item(self.split)
        else:
            self.split = None

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount_raw = self.amount.value.strip()
            amount_local = float(amount_raw)
        except ValueError:
            await interaction.response.send_message(
                f"金額は数字で入力してください（入力値: {self.amount.value}）", ephemeral=True
            )
            return

        currency = (self.currency.value or "JPY").strip().upper()
        tags = self.tags.value.strip()
        memo = self.memo.value.strip()
        split_on = bool(self.split and self.split.value.strip().lower() == "on")

        st = get_store()
        channel_id = interaction.channel_id
        country = st.get_country_for_channel(channel_id) or DEFAULT_COUNTRY

        try:
            amount_jpy, rate = fx.convert_to_jpy(amount_local, currency)
        except Exception as e:
            await interaction.response.send_message(
                f"為替レートの取得に失敗しました: {e}\n通貨コードを確認してください。",
                ephemeral=True,
            )
            return

        entry_id = st.add_entry(
            entry_type=self.entry_type,
            channel_id=channel_id,
            country=country,
            amount_local=amount_local,
            currency=currency,
            rate=rate,
            amount_jpy=amount_jpy,
            tags=tags,
            memo=memo,
            split=split_on,
            payer_id=str(interaction.user.id) if split_on else "",
            participants="",
        )

        embed = discord.Embed(
            title=f"✅ 記録しました (ID: {entry_id})",
            color=discord.Color.green() if self.entry_type == "収入" else discord.Color.orange(),
        )
        embed.add_field(name="種別", value=self.entry_type, inline=True)
        embed.add_field(name="国", value=country, inline=True)
        if currency != "JPY":
            embed.add_field(
                name="金額", value=f"{amount_local:,.0f} {currency} → 約{amount_jpy:,.0f}円", inline=False
            )
        else:
            embed.add_field(name="金額", value=f"{amount_jpy:,.0f}円", inline=False)
        if tags:
            embed.add_field(name="タグ", value=tags, inline=True)
        if memo:
            embed.add_field(name="メモ", value=memo, inline=False)
        if country == DEFAULT_COUNTRY:
            embed.set_footer(text="国が未設定のチャンネルです。/log_ed で後から修正できます。")

        await interaction.response.send_message(embed=embed)

        if split_on:
            view = ParticipantSelectView(entry_id=entry_id, payer_id=interaction.user.id)
            await interaction.followup.send(
                "割り勘の参加者を選んでください（支払者は自動的に含まれます）:",
                view=view,
            )


class ParticipantSelectView(discord.ui.View):
    def __init__(self, entry_id: int, payer_id: int):
        super().__init__(timeout=180)
        self.entry_id = entry_id
        self.payer_id = payer_id

        select = discord.ui.UserSelect(
            placeholder="参加者を選択（複数選択可）", min_values=1, max_values=10
        )
        select.callback = self.on_select
        self.add_item(select)

    async def on_select(self, interaction: discord.Interaction):
        chosen_ids = set()
        for item in self.children:
            if isinstance(item, discord.ui.UserSelect):
                chosen_ids = {str(u.id) for u in item.values}

        chosen_ids.add(str(self.payer_id))  # 支払者は必ず含める
        participants_str = ",".join(sorted(chosen_ids))

        st = get_store()
        st.update_split_participants(self.entry_id, participants_str)

        names = ", ".join(f"<@{uid}>" for uid in chosen_ids)
        await interaction.response.edit_message(
            content=f"✅ ID {self.entry_id} の割り勘参加者を設定しました: {names}",
            view=None,
        )


# ---------------------------------------------------------------------------
# スラッシュコマンド
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        log.info(f"synced {len(synced)} commands")
    except Exception as e:
        log.exception(e)
    log.info(f"Logged in as {bot.user}")


@bot.tree.command(name="log_ex", description="支出を記録します")
async def log_ex(interaction: discord.Interaction):
    await interaction.response.send_modal(EntryModal("支出"))


@bot.tree.command(name="log_in", description="収入を記録します")
async def log_in(interaction: discord.Interaction):
    await interaction.response.send_modal(EntryModal("収入"))


@bot.tree.command(name="log_ct", description="このチャンネルを国/地域に紐付けます")
@app_commands.describe(country="紐付ける国/地域名（例: タイ）")
async def log_ct(interaction: discord.Interaction, country: str):
    st = get_store()
    st.bind_channel_country(interaction.channel_id, country)
    await interaction.response.send_message(
        f"✅ このチャンネルを「{country}」に固定しました。以後 /log_ex や /log_in で国は自動設定されます。"
    )


def _split_summary(entry: dict) -> str:
    """割り勘対象の記録なら、支払者・参加者を短い文字列にして返す（対象外なら空文字）"""
    if entry.get("割り勘") != "ON":
        return ""
    payer = _valid_discord_id(entry.get("支払者ID", ""))
    participants = _participant_ids(entry)
    if not payer:
        return "  ⚠️割り勘ONですが支払者未設定"
    if not participants:
        return f"  ⚠️参加者IDが未設定または破損（支払者:<@{payer}>）"
    others = [p for p in participants if p != payer]
    others_str = " ".join(f"<@{p}>" for p in others) if others else "なし"
    return f"  💰割り勘 支払者:<@{payer}> 参加者:{others_str}"


def _match_entry(entry: dict, country: str | None, tag: str | None) -> bool:
    if country and entry.get("国") != country:
        return False
    if tag:
        entry_tags = [t.strip() for t in str(entry.get("タグ", "")).split(",") if t.strip()]
        if tag not in entry_tags:
            return False
    return True


@bot.tree.command(name="log_sum", description="国・タグで絞り込んで集計します")
@app_commands.describe(country="絞り込む国（省略可）", tag="絞り込むタグ（省略可）")
async def log_sum(interaction: discord.Interaction, country: str | None = None, tag: str | None = None):
    st = get_store()
    entries = st.get_all_entries()
    filtered = [e for e in entries if _match_entry(e, country, tag)]

    if not filtered:
        await interaction.response.send_message("該当する記録がありませんでした。")
        return

    total_expense = sum(e["円換算額"] for e in filtered if e["種別"] == "支出")
    total_income = sum(e["円換算額"] for e in filtered if e["種別"] == "収入")

    title_parts = []
    if country:
        title_parts.append(country)
    if tag:
        title_parts.append(f"#{tag}")
    title = " / ".join(title_parts) if title_parts else "全期間"

    embed = discord.Embed(title=f"📊 集計: {title}", color=discord.Color.blue())
    embed.add_field(name="支出合計", value=f"{total_expense:,.0f}円", inline=True)
    embed.add_field(name="収入合計", value=f"{total_income:,.0f}円", inline=True)
    embed.add_field(name="件数", value=f"{len(filtered)}件", inline=True)

    # 直近5件だけ内訳表示
    recent = filtered[-5:]
    lines = []
    for e in recent:
        sign = "-" if e["種別"] == "支出" else "+"
        line = f"[{e['ID']}] {sign}{e['円換算額']:,.0f}円 {e.get('タグ','')} {e.get('メモ','')}"
        line += _split_summary(e)
        lines.append(line)
    if lines:
        embed.add_field(name="直近の記録", value="\n".join(lines), inline=False)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="log_all", description="全体サマリー（国別内訳つき）")
async def log_all(interaction: discord.Interaction):
    st = get_store()
    entries = st.get_all_entries()
    if not entries:
        await interaction.response.send_message("記録がまだありません。")
        return

    total_expense = sum(e["円換算額"] for e in entries if e["種別"] == "支出")
    total_income = sum(e["円換算額"] for e in entries if e["種別"] == "収入")

    by_country = defaultdict(float)
    for e in entries:
        if e["種別"] == "支出":
            by_country[e.get("国", DEFAULT_COUNTRY)] += e["円換算額"]

    embed = discord.Embed(title="🌍 全体サマリー", color=discord.Color.purple())
    embed.add_field(name="支出合計", value=f"{total_expense:,.0f}円", inline=True)
    embed.add_field(name="収入合計", value=f"{total_income:,.0f}円", inline=True)
    embed.add_field(name="収支", value=f"{total_income - total_expense:,.0f}円", inline=True)

    country_lines = [f"{c}: {v:,.0f}円" for c, v in sorted(by_country.items(), key=lambda x: -x[1])]
    if country_lines:
        embed.add_field(name="国別支出", value="\n".join(country_lines), inline=False)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="log_ls", description="ID付きで記録を一覧表示します（直近20件）")
@app_commands.describe(country="絞り込む国（省略可）")
async def log_ls(interaction: discord.Interaction, country: str | None = None):
    st = get_store()
    entries = st.get_all_entries()
    filtered = [e for e in entries if _match_entry(e, country, None)]
    filtered = filtered[-20:]

    if not filtered:
        await interaction.response.send_message("該当する記録がありませんでした。")
        return

    lines = []
    for e in filtered:
        sign = "-" if e["種別"] == "支出" else "+"
        line = (
            f"**[{e['ID']}]** {e['日時']} {sign}{e['円換算額']:,.0f}円 {e.get('国','')} "
            f"{e.get('タグ','')} {e.get('メモ','')}"
        )
        split_note = _split_summary(e)
        if split_note:
            line += "\n" + split_note
        lines.append(line)

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[-4000:]

    embed = discord.Embed(title="📋 記録一覧", description=text, color=discord.Color.blue())
    await interaction.response.send_message(embed=embed)


FIELD_CHOICES = [
    app_commands.Choice(name="現地金額", value="現地金額"),
    app_commands.Choice(name="通貨", value="通貨"),
    app_commands.Choice(name="タグ", value="タグ"),
    app_commands.Choice(name="メモ", value="メモ"),
    app_commands.Choice(name="国", value="国"),
    app_commands.Choice(name="割り勘ON/OFF", value="割り勘"),
    app_commands.Choice(name="支払者", value="支払者ID"),
    app_commands.Choice(name="参加者", value="参加者ID"),
]

def _parse_mentions(value: str) -> list[str]:
    """'@user1 @user2' のようなメンション文字列からユーザーIDのリストを取り出す"""
    return MENTION_RE.findall(value)


@bot.tree.command(name="log_ed", description="IDを指定して記録を修正します")
@app_commands.describe(
    id="修正したい記録のID",
    field="修正する項目",
    value="新しい値（支払者/参加者はDiscordのメンション @ユーザー名 で指定してください）",
)
@app_commands.choices(field=FIELD_CHOICES)
async def log_ed(interaction: discord.Interaction, id: int, field: app_commands.Choice[str], value: str):
    st = get_store()
    entries = st.get_all_entries()
    target = next((e for e in entries if str(e["ID"]) == str(id)), None)
    if target is None:
        await interaction.response.send_message(f"ID {id} が見つかりませんでした。", ephemeral=True)
        return

    # 金額 or 通貨を変更した場合は円換算額とレートも再計算する
    if field.value in ("現地金額", "通貨"):
        amount_local = float(value) if field.value == "現地金額" else float(target["現地金額"])
        currency = value.upper() if field.value == "通貨" else target["通貨"]

        try:
            amount_jpy, rate = fx.convert_to_jpy(amount_local, currency)
        except Exception as e:
            await interaction.response.send_message(f"為替レート取得に失敗しました: {e}", ephemeral=True)
            return

        st.update_field(id, "現地金額", str(amount_local))
        st.update_field(id, "通貨", currency)
        st.update_field(id, "レート", str(rate))
        st.update_field(id, "円換算額", str(amount_jpy))

        await interaction.response.send_message(
            f"✅ ID {id} を更新しました: {amount_local} {currency} → {amount_jpy:,.0f}円"
        )
        return

    if field.value == "割り勘":
        normalized = value.strip().upper()
        if normalized not in ("ON", "OFF"):
            await interaction.response.send_message("割り勘の値は ON か OFF で指定してください。", ephemeral=True)
            return
        st.update_field(id, "割り勘", normalized)
        await interaction.response.send_message(f"✅ ID {id} の割り勘を {normalized} にしました。")
        return

    if field.value == "支払者ID":
        ids = _parse_mentions(value)
        if not ids:
            await interaction.response.send_message(
                "支払者は @ユーザー名 の形式で1人だけメンションしてください。", ephemeral=True
            )
            return
        st.update_field(id, "支払者ID", ids[0])
        await interaction.response.send_message(f"✅ ID {id} の支払者を <@{ids[0]}> にしました。")
        return

    if field.value == "参加者ID":
        ids = _parse_mentions(value)
        if not ids:
            await interaction.response.send_message(
                "参加者は @ユーザー名 の形式でメンションしてください（複数可）。", ephemeral=True
            )
            return
        payer = str(target.get("支払者ID", "")).strip()
        if payer:
            ids.append(payer)
        ids = sorted(set(ids))
        st.update_field(id, "参加者ID", ",".join(ids))
        names = ", ".join(f"<@{uid}>" for uid in ids)
        await interaction.response.send_message(f"✅ ID {id} の参加者を更新しました: {names}")
        return

    ok = st.update_field(id, field.value, value)
    if ok:
        await interaction.response.send_message(f"✅ ID {id} の「{field.value}」を更新しました: {value}")
    else:
        await interaction.response.send_message(f"ID {id} が見つかりませんでした。", ephemeral=True)


@bot.tree.command(name="log_rm", description="IDを指定して記録を削除します")
@app_commands.describe(id="削除したい記録のID")
async def log_rm(interaction: discord.Interaction, id: int):
    st = get_store()
    ok = st.delete_entry(id)
    if ok:
        await interaction.response.send_message(f"🗑️ ID {id} を削除しました。")
    else:
        await interaction.response.send_message(f"ID {id} が見つかりませんでした。", ephemeral=True)


@bot.tree.command(name="log_wari", description="割り勘の精算額を計算します")
@app_commands.describe(country="絞り込む国（省略可）")
async def log_wari(interaction: discord.Interaction, country: str | None = None):
    st = get_store()
    entries = st.get_all_entries()

    split_entries = [
        e for e in entries
        if e.get("割り勘") == "ON" and e.get("種別") == "支出" and (not country or e.get("国") == country)
    ]

    if not split_entries:
        await interaction.response.send_message("割り勘対象の記録がありませんでした。")
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
            await interaction.response.send_message(
                f"⚠️ ID {ids} の参加者データが破損しています。"
                " `/log_ed` で参加者をメンション指定し直してください。",
                ephemeral=True,
            )
            return
        await interaction.response.send_message("精算の必要はありません（すでに帳尻が合っています）。")
        return

    lines = [f"<@{a}> → <@{b}> に {amt:,.0f}円" for a, b, amt in transactions]
    title = f"💰 割り勘精算" + (f"（{country}）" if country else "")
    embed = discord.Embed(title=title, description="\n".join(lines), color=discord.Color.gold())
    if invalid_entry_ids:
        embed.set_footer(
            text=f"参加者データ破損のため除外: ID {', '.join(invalid_entry_ids)}"
        )
    await interaction.response.send_message(embed=embed)


if __name__ == "__main__":
    token = os.environ["DISCORD_BOT_TOKEN"]
    bot.run(token)
