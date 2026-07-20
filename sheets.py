"""
Google Sheets 操作モジュール
2つのタブを使う:
  - "Log"      : 収支の記録本体
  - "Channels" : チャンネルID <-> 国 の紐付け
存在しない場合は自動で作成する。
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


def _get_client():
    creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


class SheetStore:
    def __init__(self):
        self.client = _get_client()
        sheet_id = os.environ["GOOGLE_SHEET_ID"]
        self.ss = self.client.open_by_key(sheet_id)
        self.log_ws = self._get_or_create_ws("Log", LOG_HEADERS)
        self.channel_ws = self._get_or_create_ws("Channels", CHANNEL_HEADERS)

    def _get_or_create_ws(self, title, headers):
        try:
            ws = self.ss.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self.ss.add_worksheet(title=title, rows=1000, cols=len(headers) + 2)
            ws.append_row(headers)
            return ws
        # ヘッダーが無ければ追加
        first_row = ws.row_values(1)
        if not first_row:
            ws.append_row(headers)
        return ws

    # ---------- Channel <-> Country ----------

    def bind_channel_country(self, channel_id: str, country: str):
        records = self.channel_ws.get_all_records()
        for i, row in enumerate(records, start=2):  # 2行目からデータ
            if str(row["チャンネルID"]) == str(channel_id):
                self.channel_ws.update_cell(i, 2, country)
                return
        self.channel_ws.append_row([str(channel_id), country])

    def get_country_for_channel(self, channel_id: str) -> str | None:
        records = self.channel_ws.get_all_records()
        for row in records:
            if str(row["チャンネルID"]) == str(channel_id):
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
    ) -> int:
        new_id = self._next_id()
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        row = [
            new_id, entry_type, now, str(channel_id), country,
            amount_local, currency.upper(), rate, amount_jpy,
            tags, memo, "ON" if split else "OFF", str(payer_id), participants,
        ]
        self.log_ws.append_row(row)
        return new_id

    def get_all_entries(self) -> list[dict]:
        return self.log_ws.get_all_records()

    def find_row_by_id(self, entry_id: int) -> int | None:
        ids = self.log_ws.col_values(1)[1:]
        for i, v in enumerate(ids, start=2):
            if str(v) == str(entry_id):
                return i
        return None

    def update_field(self, entry_id: int, field: str, value: str) -> bool:
        row_idx = self.find_row_by_id(entry_id)
        if row_idx is None:
            return False
        if field not in LOG_HEADERS:
            return False
        col_idx = LOG_HEADERS.index(field) + 1
        self.log_ws.update_cell(row_idx, col_idx, value)
        return True

    def update_split_participants(self, entry_id: int, participants: str):
        row_idx = self.find_row_by_id(entry_id)
        if row_idx is None:
            return False
        col_idx = LOG_HEADERS.index("参加者ID") + 1
        self.log_ws.update_cell(row_idx, col_idx, participants)
        return True

    def delete_entry(self, entry_id: int) -> bool:
        row_idx = self.find_row_by_id(entry_id)
        if row_idx is None:
            return False
        self.log_ws.delete_rows(row_idx)
        return True
