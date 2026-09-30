"""
Google Sheets 操作モジュール
使うタブ:
  - "Log"         : 収支の記録本体
  - "Channels"    : チャンネルID <-> 国 の紐付け
  - "Tags"        : 選択用タグ一覧
  - "Memos"       : ユーザー固有の買い物メモ
  - "Settlements" : 建て替え確定スナップショット
存在しない場合は自動で作成する。

【重要】DiscordのIDは18〜19桁なので、Sheetsには必ずRAW文字列として書き込む。
特にカンマ区切りの参加者IDをUSER_ENTEREDで書くと巨大な数値として解釈され、
精度が失われて正しいメンションに戻せなくなる。
また読み取り時、gspreadのnumericiseはカンマを千位区切りとして削除するため、
ID列は numericise_ignore で除外する必要がある。
"""
import os
import re
import json
import datetime
import gspread
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
]

LOG_HEADERS = [
    "ID", "種別", "日時", "チャンネルID", "国",
    "現地金額", "通貨", "レート", "円換算額",
    "タグ", "メモ", "割り勘", "支払者ID", "参加者ID", "集計対象",
    "予算ユーザーID",
]
CHANNEL_HEADERS = ["チャンネルID", "国", "通貨"]
TAG_HEADERS = ["タグ"]
MEMO_HEADERS = [
    "ID", "日時", "ユーザーID", "国",
    "現地金額", "通貨", "レート", "円換算額", "メモ", "集計対象",
]
SETTLEMENT_HEADERS = [
    "ID", "確定日", "バッチID", "債務者ID", "債権者ID", "金額円",
    "メモ", "作成者ID", "国フィルタ",
]

ID_LIKE_FIELDS = {"チャンネルID", "支払者ID", "参加者ID", "予算ユーザーID"}
MEMO_ID_LIKE_FIELDS = {"ユーザーID"}
SETTLEMENT_ID_LIKE_FIELDS = {"債務者ID", "債権者ID", "作成者ID"}


def _get_client():
    creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _clean_id_text(value) -> str:
    """過去にテキスト化のため付けたアポストロフィを読み取り時に除く。"""
    return str(value).strip().lstrip("'")


def _normalize_channel_id(value) -> str:
    """Sheets上で数値化されたDiscord IDを文字列IDに戻す。"""
    text = _clean_id_text(value)
    if not text:
        return ""
    try:
        if re.search(r"[eE.]", text):
            return str(int(float(text)))
    except (ValueError, OverflowError):
        pass
    if text.endswith(".0"):
        base = text[:-2]
        if base.isdigit():
            return base
    return text


def _id_column_indexes(headers: list[str], fields: set[str] | None = None) -> list[int]:
    """gspreadのnumericiseがカンマを千位区切りとして消すのを避ける列番号(1始まり)。"""
    target = fields if fields is not None else ID_LIKE_FIELDS
    return [headers.index(name) + 1 for name in target if name in headers]


class SheetStore:
    def __init__(self):
        self.client = _get_client()
        sheet_id = os.environ["GOOGLE_SHEET_ID"]
        self.ss = self.client.open_by_key(sheet_id)
        self.log_ws = self._get_or_create_ws("Log", LOG_HEADERS)
        self.channel_ws = self._get_or_create_ws("Channels", CHANNEL_HEADERS)
        self.tag_ws = self._get_or_create_ws("Tags", TAG_HEADERS)
        self.memo_ws = self._get_or_create_ws("Memos", MEMO_HEADERS)
        self.settlement_ws = self._get_or_create_ws("Settlements", SETTLEMENT_HEADERS)
        self._ensure_log_headers()
        self._ensure_channel_headers()
        self._ensure_memo_headers()
        self._ensure_settlement_headers()
        self._ensure_default_tags()

    def _ensure_channel_headers(self):
        """既存Channelsシートに通貨列が無ければヘッダーを拡張する。"""
        first_row = self.channel_ws.row_values(1)
        if not first_row:
            self.channel_ws.append_row(CHANNEL_HEADERS, value_input_option="RAW")
            return
        missing = [h for h in CHANNEL_HEADERS if h not in first_row]
        if missing:
            self.channel_ws.update([first_row + missing], "A1", raw=True)

    def _ensure_log_headers(self):
        """既存Logシートに新列が無ければヘッダーを拡張する。"""
        first_row = self.log_ws.row_values(1)
        if not first_row:
            self.log_ws.append_row(LOG_HEADERS, value_input_option="RAW")
            return
        missing = [h for h in LOG_HEADERS if h not in first_row]
        if missing:
            self.log_ws.update([first_row + missing], "A1", raw=True)

    def _ensure_memo_headers(self):
        """既存Memosシートに新列が無ければヘッダーを拡張する。"""
        first_row = self.memo_ws.row_values(1)
        if not first_row:
            self.memo_ws.append_row(MEMO_HEADERS, value_input_option="RAW")
            return
        missing = [h for h in MEMO_HEADERS if h not in first_row]
        if missing:
            self.memo_ws.update([first_row + missing], "A1", raw=True)

    def _ensure_settlement_headers(self):
        """既存Settlementsシートに新列が無ければヘッダーを拡張する。"""
        first_row = self.settlement_ws.row_values(1)
        if not first_row:
            self.settlement_ws.append_row(SETTLEMENT_HEADERS, value_input_option="RAW")
            return
        missing = [h for h in SETTLEMENT_HEADERS if h not in first_row]
        if missing:
            self.settlement_ws.update([first_row + missing], "A1", raw=True)

    def _get_or_create_ws(self, title, headers):
        try:
            ws = self.ss.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self.ss.add_worksheet(title=title, rows=1000, cols=len(headers) + 2)
            ws.append_row(headers, value_input_option="RAW")
            return ws
        # ヘッダーが無ければ追加
        first_row = ws.row_values(1)
        if not first_row:
            ws.append_row(headers, value_input_option="RAW")
        return ws

    def _ensure_default_tags(self):
        """Tagsシートが空ならよく使うタグを初期投入する。"""
        if self.list_tags():
            return
        defaults = ["飯", "移動", "宿", "観光", "買い物", "両替", "その他"]
        rows = [[t] for t in defaults]
        self.tag_ws.append_rows(rows, value_input_option="RAW")

    # ---------- Channel <-> Country ----------

    def bind_channel_country(
        self, channel_id: str | int, country: str, currency: str | None = None
    ):
        country = str(country).strip()
        currency = str(currency or "").strip().upper()
        target_id = _normalize_channel_id(channel_id)
        records = self.channel_ws.get_all_records(
            numericise_ignore=_id_column_indexes(CHANNEL_HEADERS)
        )
        for i, row in enumerate(records, start=2):  # 2行目からデータ
            if _normalize_channel_id(row["チャンネルID"]) == target_id:
                self.channel_ws.update(
                    [[country, currency]], range_name=f"B{i}:C{i}", raw=True
                )
                return
        self.channel_ws.append_row(
            [target_id, country, currency], value_input_option="RAW"
        )

    def get_channel_binding(self, channel_id: str | int) -> dict | None:
        target_id = _normalize_channel_id(channel_id)
        if not target_id:
            return None
        records = self.channel_ws.get_all_records(
            numericise_ignore=_id_column_indexes(CHANNEL_HEADERS)
        )
        for row in records:
            if _normalize_channel_id(row["チャンネルID"]) != target_id:
                continue
            country = str(row.get("国", "")).strip()
            if not country:
                return None
            return {
                "国": country,
                "通貨": str(row.get("通貨", "")).strip().upper(),
            }
        return None

    def resolve_channel_binding(self, *channel_ids: str | int) -> dict | None:
        """複数チャンネルIDを順に試し、最初に見つかった国設定を返す。"""
        for channel_id in channel_ids:
            binding = self.get_channel_binding(channel_id)
            if binding:
                return binding
        return None

    def get_country_for_channel(self, channel_id: str | int) -> str | None:
        binding = self.get_channel_binding(channel_id)
        return binding["国"] if binding else None

    # ---------- Log entries ----------

    def _next_id(self) -> int:
        ids = self.log_ws.col_values(1)[1:]  # ヘッダー除く
        nums = [int(x) for x in ids if str(x).isdigit()]
        return max(nums, default=0) + 1

    def add_entry(
        self,
        entry_type: str,  # "支出" or "予算"（旧データは「収入」もあり得る）
        channel_id: str,
        country: str,
        amount_local: float,
        currency: str,
        rate: float,
        amount_jpy: float,
        tags: str,
        memo: str,
        split: bool,
        payer_id: str,
        participants: str,
        recorded_at: str | None = None,
        include_in_sum: bool = False,
        budget_owner_id: str = "",
    ) -> int:
        new_id = self._next_id()
        now = recorded_at or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        row = [
            new_id, entry_type, now, str(channel_id), country,
            amount_local, currency.upper(), rate, amount_jpy,
            tags, memo, "ON" if split else "OFF",
            str(payer_id) if payer_id else "",
            str(participants),
            "ON" if include_in_sum else "OFF",
            str(budget_owner_id) if budget_owner_id else "",
        ]
        self.log_ws.append_row(row, value_input_option="RAW")
        return new_id

    def get_all_entries(self) -> list[dict]:
        # 参加者IDは「id1,id2」形式。numericiseするとカンマが消えて1つの巨大数値になる。
        records = self.log_ws.get_all_records(
            numericise_ignore=_id_column_indexes(LOG_HEADERS)
        )
        for row in records:
            for field in ID_LIKE_FIELDS:
                row[field] = _clean_id_text(row.get(field, ""))
            if "予算ユーザーID" not in row:
                row["予算ユーザーID"] = ""
        return records

    def find_row_by_id(self, entry_id: int) -> int | None:
        ids = self.log_ws.col_values(1)[1:]
        for i, v in enumerate(ids, start=2):
            if str(v) == str(entry_id):
                return i
        return None

    def update_field(self, entry_id: int, field: str, value) -> bool:
        row_idx = self.find_row_by_id(entry_id)
        if row_idx is None:
            return False
        if field not in LOG_HEADERS:
            return False
        col_idx = LOG_HEADERS.index(field) + 1
        write_value = str(value) if field in ID_LIKE_FIELDS else value
        cell = gspread.utils.rowcol_to_a1(row_idx, col_idx)
        self.log_ws.update([[write_value]], range_name=cell, raw=True)
        return True

    def update_split_participants(self, entry_id: int, participants: str):
        row_idx = self.find_row_by_id(entry_id)
        if row_idx is None:
            return False
        col_idx = LOG_HEADERS.index("参加者ID") + 1
        cell = gspread.utils.rowcol_to_a1(row_idx, col_idx)
        self.log_ws.update([[str(participants)]], range_name=cell, raw=True)
        return True

    def delete_entry(self, entry_id: int) -> bool:
        row_idx = self.find_row_by_id(entry_id)
        if row_idx is None:
            return False
        self.log_ws.delete_rows(row_idx)
        return True

    # ---------- Tags ----------

    def list_tags(self) -> list[str]:
        values = self.tag_ws.col_values(1)[1:]  # ヘッダー除く
        tags = []
        seen = set()
        for v in values:
            tag = str(v).strip()
            if tag and tag not in seen:
                seen.add(tag)
                tags.append(tag)
        return tags

    def add_tag(self, tag: str) -> bool:
        tag = tag.strip()
        if not tag:
            return False
        existing = {t.lower() for t in self.list_tags()}
        if tag.lower() in existing:
            return False
        self.tag_ws.append_row([tag], value_input_option="RAW")
        return True

    def remove_tag(self, tag: str) -> bool:
        tag = tag.strip().lower()
        values = self.tag_ws.col_values(1)[1:]
        for i, v in enumerate(values, start=2):
            if str(v).strip().lower() == tag:
                self.tag_ws.delete_rows(i)
                return True
        return False

    # ---------- Personal shopping memos ----------

    def _next_memo_id(self) -> int:
        ids = self.memo_ws.col_values(1)[1:]
        nums = [int(x) for x in ids if str(x).isdigit()]
        return max(nums, default=0) + 1

    def add_memo(
        self,
        user_id: str,
        country: str,
        amount_local: float,
        currency: str,
        rate: float,
        amount_jpy: float,
        memo: str,
        recorded_at: str | None = None,
        include_in_sum: bool = False,
    ) -> int:
        new_id = self._next_memo_id()
        now = recorded_at or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        row = [
            new_id,
            now,
            str(user_id),
            country,
            amount_local,
            currency.upper(),
            rate,
            amount_jpy,
            memo,
            "ON" if include_in_sum else "OFF",
        ]
        self.memo_ws.append_row(row, value_input_option="RAW")
        return new_id

    def get_all_memos(self) -> list[dict]:
        records = self.memo_ws.get_all_records(
            numericise_ignore=_id_column_indexes(MEMO_HEADERS, MEMO_ID_LIKE_FIELDS)
        )
        for row in records:
            row["ユーザーID"] = _clean_id_text(row.get("ユーザーID", ""))
            if "集計対象" not in row or str(row.get("集計対象", "")).strip() == "":
                row["集計対象"] = "OFF"
            try:
                row["円換算額"] = float(row.get("円換算額") or 0)
            except (TypeError, ValueError):
                row["円換算額"] = 0.0
        return records

    def get_memos_for_user(self, user_id: str) -> list[dict]:
        target = _clean_id_text(user_id)
        return [m for m in self.get_all_memos() if m.get("ユーザーID") == target]

    def find_memo_row_by_id(self, memo_id: int) -> int | None:
        ids = self.memo_ws.col_values(1)[1:]
        for i, v in enumerate(ids, start=2):
            if str(v) == str(memo_id):
                return i
        return None

    def get_memo_by_id(self, memo_id: int) -> dict | None:
        for memo in self.get_all_memos():
            if str(memo.get("ID")) == str(memo_id):
                return memo
        return None

    def update_memo_field(self, memo_id: int, field: str, value) -> bool:
        row_idx = self.find_memo_row_by_id(memo_id)
        if row_idx is None:
            return False
        if field not in MEMO_HEADERS:
            return False
        col_idx = MEMO_HEADERS.index(field) + 1
        write_value = str(value) if field in MEMO_ID_LIKE_FIELDS else value
        cell = gspread.utils.rowcol_to_a1(row_idx, col_idx)
        self.memo_ws.update([[write_value]], range_name=cell, raw=True)
        return True

    def delete_memo(self, memo_id: int) -> bool:
        row_idx = self.find_memo_row_by_id(memo_id)
        if row_idx is None:
            return False
        self.memo_ws.delete_rows(row_idx)
        return True

    # ---------- Settlements (建て替え確定スナップショット) ----------

    def _next_settlement_id(self) -> int:
        ids = self.settlement_ws.col_values(1)[1:]
        nums = [int(x) for x in ids if str(x).isdigit()]
        return max(nums, default=0) + 1

    def _next_settlement_batch_id(self) -> int:
        records = self.get_all_settlements()
        nums = []
        for row in records:
            raw = str(row.get("バッチID", "")).strip()
            if raw.isdigit():
                nums.append(int(raw))
        return max(nums, default=0) + 1

    def add_settlement_batch(
        self,
        transactions: list[tuple[str, str, float]],
        creator_id: str,
        memo: str = "",
        country_filter: str = "",
        settled_at: str | None = None,
    ) -> int:
        """送金リストを1バッチとして保存。バッチIDを返す。"""
        if not transactions:
            raise ValueError("transactions must not be empty")
        batch_id = self._next_settlement_batch_id()
        now = settled_at or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        rows = []
        next_id = self._next_settlement_id()
        for debtor_id, creditor_id, amount in transactions:
            rows.append([
                next_id,
                now,
                batch_id,
                str(debtor_id),
                str(creditor_id),
                float(amount),
                memo,
                str(creator_id),
                country_filter or "",
            ])
            next_id += 1
        self.settlement_ws.append_rows(rows, value_input_option="RAW")
        return batch_id

    def get_all_settlements(self) -> list[dict]:
        records = self.settlement_ws.get_all_records(
            numericise_ignore=_id_column_indexes(
                SETTLEMENT_HEADERS, SETTLEMENT_ID_LIKE_FIELDS
            )
        )
        for row in records:
            for field in SETTLEMENT_ID_LIKE_FIELDS:
                row[field] = _clean_id_text(row.get(field, ""))
            try:
                row["金額円"] = float(row.get("金額円") or 0)
            except (TypeError, ValueError):
                row["金額円"] = 0.0
        return records

    def get_latest_settlement_batch(self) -> list[dict]:
        """最新バッチの行一覧（確定日・バッチIDが最大のもの）。"""
        records = self.get_all_settlements()
        if not records:
            return []
        best_key = None
        best_batch = ""
        for row in records:
            batch = str(row.get("バッチID", "")).strip()
            settled = str(row.get("確定日", "")).strip()
            try:
                batch_num = int(batch) if batch.isdigit() else -1
            except ValueError:
                batch_num = -1
            key = (settled, batch_num)
            if best_key is None or key > best_key:
                best_key = key
                best_batch = batch
        if not best_batch:
            return []
        return [r for r in records if str(r.get("バッチID", "")).strip() == best_batch]
