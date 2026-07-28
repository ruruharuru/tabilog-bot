"""
国名 → ISO 4217 通貨コードの対応表。
/log_ct で登録した国名から、通貨欄が空のときに自動で通貨を推定する。
日本語名・英語名・一般的な別名に対応。
"""
import re
import unicodedata

# 正規化した国名 -> 通貨コード
_COUNTRY_CURRENCY: dict[str, str] = {}

_COUNTRY_SUFFIX_RE = re.compile(
    r"(王国|共和国|民主共和国|連邦|公国|国|地方|地域|地区)$"
)

def _add(names: list[str], currency: str) -> None:
    for name in names:
        _COUNTRY_CURRENCY[name.strip().lower()] = currency


# アジア
_add(["日本", "japan"], "JPY")
_add(["タイ", "thailand"], "THB")
_add(["ベトナム", "vietnam"], "VND")
_add(["カンボジア", "cambodia"], "KHR")
_add(["ラオス", "laos"], "LAK")
_add(["ミャンマー", "myanmar", "ビルマ", "burma"], "MMK")
_add(["マレーシア", "malaysia"], "MYR")
_add(["シンガポール", "singapore"], "SGD")
_add(["インドネシア", "indonesia"], "IDR")
_add(["フィリピン", "philippines"], "PHP")
_add(["インド", "india"], "INR")
_add(["ネパール", "nepal"], "NPR")
_add(["スリランカ", "sri lanka", "srilanka"], "LKR")
_add(["バングラデシュ", "bangladesh"], "BDT")
_add(["中国", "china"], "CNY")
_add(["台湾", "taiwan"], "TWD")
_add(["香港", "hong kong", "hongkong"], "HKD")
_add(["マカオ", "macau", "macao"], "MOP")
_add(["韓国", "korea", "south korea", "大韓民国"], "KRW")
_add(["モンゴル", "mongolia"], "MNT")
_add(["ブルネイ", "brunei"], "BND")
_add(["パキスタン", "pakistan"], "PKR")
_add(["カザフスタン", "kazakhstan"], "KZT")
_add(["ウズベキスタン", "uzbekistan"], "UZS")
_add(["ジョージア", "georgia"], "GEL")
_add(["アラブ首長国連邦", "uae", "ドバイ", "dubai"], "AED")
_add(["サウジアラビア", "saudi arabia"], "SAR")
_add(["イスラエル", "israel"], "ILS")
_add(["ヨルダン", "jordan"], "JOD")
_add(["レバノン", "lebanon"], "LBP")
_add(["イラン", "iran"], "IRR")
_add(["イラク", "iraq"], "IQD")

# 北米・中南米
_add(["アメリカ", "america", "usa", "us", "米国", "合衆国"], "USD")
_add(["カナダ", "canada"], "CAD")
_add(["メキシコ", "mexico"], "MXN")
_add(["ブラジル", "brazil"], "BRL")
_add(["アルゼンチン", "argentina"], "ARS")
_add(["チリ", "chile"], "CLP")
_add(["ペルー", "peru"], "PEN")
_add(["コロンビア", "colombia"], "COP")
_add(["エクアドル", "ecuador"], "USD")
_add(["ボリビア", "bolivia"], "BOB")
_add(["パラグアイ", "paraguay"], "PYG")
_add(["ウルグアイ", "uruguay"], "UYU")
_add(["ベネズエラ", "venezuela"], "VES")
_add(["コスタリカ", "costa rica"], "CRC")
_add(["パナマ", "panama"], "PAB")
_add(["キューバ", "cuba"], "CUP")
_add(["ドミニカ共和国", "dominican republic"], "DOP")
_add(["ジャマイカ", "jamaica"], "JMD")
_add(["グアテマラ", "guatemala"], "GTQ")

# ヨーロッパ（ユーロ圏）
_add([
    "フランス", "france", "ドイツ", "germany", "イタリア", "italy",
    "スペイン", "spain", "オランダ", "netherlands", "ベルギー", "belgium",
    "オーストリア", "austria", "ポルトガル", "portugal", "ギリシャ", "greece",
    "アイルランド", "ireland", "フィンランド", "finland", "ルクセンブルク", "luxembourg",
    "スロバキア", "slovakia", "スロベニア", "slovenia", "エストニア", "estonia",
    "ラトビア", "latvia", "リトアニア", "lithuania", "マルタ", "malta",
    "キプロス", "cyprus", "クロアチア", "croatia",
], "EUR")

_add(["イギリス", "uk", "united kingdom", "britain", "england"], "GBP")
_add(["スイス", "switzerland"], "CHF")
_add(["スウェーデン", "sweden"], "SEK")
_add(["ノルウェー", "norway"], "NOK")
_add(["デンマーク", "denmark"], "DKK")
_add(["アイスランド", "iceland"], "ISK")
_add(["ポーランド", "poland"], "PLN")
_add(["チェコ", "czech", "czechia"], "CZK")
_add(["ハンガリー", "hungary"], "HUF")
_add(["ルーマニア", "romania"], "RON")
_add(["ブルガリア", "bulgaria"], "BGN")
_add(["セルビア", "serbia"], "RSD")
_add(["トルコ", "turkey", "turkiye"], "TRY")
_add(["ロシア", "russia"], "RUB")
_add(["ウクライナ", "ukraine"], "UAH")
_add(["モルドバ", "moldova"], "MDL")
_add(["アルバニア", "albania"], "ALL")
_add(["北マケドニア", "macedonia"], "MKD")
_add(["ボスニア・ヘルツェゴビナ", "bosnia"], "BAM")

# アフリカ
_add(["エジプト", "egypt"], "EGP")
_add(["モロッコ", "morocco"], "MAD")
_add(["チュニジア", "tunisia"], "TND")
_add(["南アフリカ", "south africa"], "ZAR")
_add(["ケニア", "kenya"], "KES")
_add(["タンザニア", "tanzania"], "TZS")
_add(["ウガンダ", "uganda"], "UGX")
_add(["エチオピア", "ethiopia"], "ETB")
_add(["ガーナ", "ghana"], "GHS")
_add(["ナイジェリア", "nigeria"], "NGN")
_add(["セネガル", "senegal"], "XOF")
_add(["ルワンダ", "rwanda"], "RWF")
_add(["ナミビア", "namibia"], "NAD")
_add(["ボツワナ", "botswana"], "BWP")
_add(["マダガスカル", "madagascar"], "MGA")
_add(["モーリシャス", "mauritius"], "MUR")

# オセアニア
_add(["オーストラリア", "australia"], "AUD")
_add(["ニュージーランド", "new zealand"], "NZD")
_add(["フィジー", "fiji"], "FJD")
_add(["パプアニューギニア", "papua new guinea"], "PGK")


def _normalize_country_name(country: str) -> str:
    text = unicodedata.normalize("NFKC", str(country).strip())
    text = _COUNTRY_SUFFIX_RE.sub("", text).strip()
    return text.lower()


def get_currency_for_country(country: str) -> str | None:
    """国名から通貨コードを返す。見つからなければ None。"""
    if not country or str(country).strip() == "不明":
        return None

    normalized = _normalize_country_name(country)
    if not normalized:
        return None

    direct = _COUNTRY_CURRENCY.get(normalized)
    if direct:
        return direct

    # 「タイ・バンコク」「タイ（バンコク）」などの表記ゆれに対応
    for part in re.split(r"[\s/・,、()（）\[\]【】]+", normalized):
        if not part:
            continue
        part = _COUNTRY_SUFFIX_RE.sub("", part).strip()
        if part in _COUNTRY_CURRENCY:
            return _COUNTRY_CURRENCY[part]

    return None
