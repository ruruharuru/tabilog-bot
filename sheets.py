"""
Google Sheets 操作モジュール
2つのタブを使う:
  - "Log"      : 収支の記録本体
  - "Channels" : チャンネルID <-> 国 の紐付け
  - "Tags"     : 選択用タグ一覧
存在しない場合は自動で作成する。

【重要】DiscordのIDは18〜19桁なので、Sheetsには必ずRAW文字列として書き込む。
特にカンマ区切りの参加者IDをUSER_ENTEREDで書くと巨大な数値として解釈され、
精度が失われて正しいメンションに戻せなくなる。
また読み取り時、gspreadのnumericiseはカンマを千位区切りとして削除するため、
ID列は numericise_ignore で除外する必要がある。
"""
import os
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
    "タグ", "メモ", "割り勘", "支払者ID", "参加者ID",
]
CHANNEL_HEADERS = ["チャンネルID", "国"]
TAG_HEADERS = ["タグ"]

ID_LIKE_FIELDS = {"チャンネルID", "支払者ID", "参加者ID"}


def _get_client():
    creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _clean_id_text(value) -> str:
    """過去にテキスト化のため付けたアポストロフィを読み取り時に除く。"""
    return str(value).strip().lstrip("'")


def _id_column_indexes(headers: list[str]) -> list[int]:
    """gspreadのnumericiseがカンマを千位区切りとして消すのを避ける列番号(1始まり)。"""
    return [headers.index(name) + 1 for name in ID_LIKE_FIELDS if name in headers]


class SheetStore:
    def __init__(self):
        self.client = _get_client()
        sheet_id = os.environ["GOOGLE_SHEET_ID"]
        self.ss = self.client.open_by_key(sheet_id)
        self.log_ws = self._get_or_create_ws("Log", LOG_HEADERS)
        self.channel_ws = self._get_or_create_ws("Channels", CHANNEL_HEADERS)
        self.tag_ws = self._get_or_create_ws("Tags", TAG_HEADERS)
        self._ensure_default_tags()

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
        defaults = ["飯", "移動", "宿", "観光", "買い物", "その他"]
        rows = [[t] for t in defaults]
        self.tag_ws.append_rows(rows, value_input_option="RAW")

    # ---------- Channel <-> Country ----------

    def bind_channel_country(self, channel_id: str, country: str):
        records = self.channel_ws.get_all_records(
            numericise_ignore=_id_column_indexes(CHANNEL_HEADERS)
        )
        for i, row in enumerate(records, start=2):  # 2行目からデータ
            if _clean_id_text(row["チャンネルID"]) == str(channel_id):
                self.channel_ws.update([[country]], range_name=f"B{i}", raw=True)
                return
        self.channel_ws.append_row(
            [str(channel_id), country], value_input_option="RAW"
        )

    def get_country_for_channel(self, channel_id: str) -> str | None:
        records = self.channel_ws.get_all_records(
            numericise_ignore=_id_column_indexes(CHANNEL_HEADERS)
        )
        for row in records:
            if _clean_id_text(row["チャンネルID"]) == str(channel_id):
                return row["国"]
        return None

    # ---------- Log entries ----------

    def _next_id(self) -> int:
        ids = self.log_ws.col_values(1)[1:]  # ヘッダー除く
        nums = [int(x) for x in ids if str(x).isdigit()]
        return max(nums, default=0) + 1

    def add_entry(
        self,
        entry_type: str,  # "支出" or "収入"
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
    ) -> int:
        new_id = self._next_id()
        now = recorded_at or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        row = [
            new_id, entry_type, now, str(channel_id), country,
            amount_local, currency.upper(), rate, amount_jpy,
            tags, memo, "ON" if split else "OFF",
            str(payer_id) if payer_id else "",
            str(participants),
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
