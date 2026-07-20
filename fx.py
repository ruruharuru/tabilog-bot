"""
為替レート取得モジュール
open.er-api.com（ExchangeRate-APIの無料オープンエンドポイント、APIキー不要）を使う。
frankfurter.app はECB基準のため主要30通貨程度しか扱えずマイナー通貨で404になっていたので、
166通貨に対応したこちらに切り替えている。
軽くキャッシュして、同じ通貨への無駄なリクエストを減らす。
"""
import time
import requests

_CACHE = {}
_CACHE_TTL = 60 * 60  # 1時間キャッシュ


def get_rate_to_jpy(currency: str) -> float:
    """
    指定した通貨1単位あたりのJPYレートを返す。
    JPY自身が指定された場合は1.0を返す。
    """
    currency = currency.upper().strip()

    if currency == "JPY":
        return 1.0

    now = time.time()
    cached = _CACHE.get(currency)
    if cached and (now - cached["time"] < _CACHE_TTL):
        return cached["rate"]

    url = f"https://open.er-api.com/v6/latest/{currency}"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    if data.get("result") != "success":
        raise ValueError(f"通貨コード '{currency}' は認識されませんでした。ISO 4217コードを確認してください。")

    rate = data.get("rates", {}).get("JPY")
    if rate is None:
        raise ValueError(f"通貨コード '{currency}' のJPYレートが見つかりませんでした")

    _CACHE[currency] = {"rate": rate, "time": now}
    return rate


def convert_to_jpy(amount: float, currency: str) -> tuple[float, float]:
    """
    (換算後のJPY金額, 適用したレート) を返す
    """
    rate = get_rate_to_jpy(currency)
    jpy = round(amount * rate)
    return jpy, rate
