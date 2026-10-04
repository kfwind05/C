from flask import Flask, jsonify, request, render_template, session, redirect, url_for, flash
from urllib.parse import urlparse, urljoin, urlencode
from functools import wraps
import json
import os
import re
import secrets
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone

# app.py はプロジェクト直下に置く。
# 実体（templates / static / data）は bousai_app/ 配下にあるので、そこを参照する。
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(BASE_DIR, 'bousai_app')

app = Flask(
    __name__,
    template_folder=os.path.join(APP_DIR, 'templates'),
    static_folder=os.path.join(APP_DIR, 'static'),
)
app.secret_key = os.environ.get('FLASK_SECRET_KEY') or secrets.token_hex(32)

# 管理者認証情報
ADMIN_CREDENTIALS = {
    'admin': '123'
}

# ────────────────────────────────
# 気象警報・注意報設定
PREFECTURE_CODE = "020000"  # 青森県
AREA_NAME = "青森市"
# 気象庁が配信する青森市の市町村コード（青森県青森市）
AREA_CODE = "0220100"

WARNING_URL = (
    f"https://www.jma.go.jp/bosai/warning/data/r8/{PREFECTURE_CODE}.json"
)

JST = timezone(timedelta(hours=9))

# 警報・注意報のコード一覧
WARNING_CODES = {
    "00": "解除",
    "02": "暴風雪警報",
    "03": "レベル3大雨警報",
    "04": "洪水警報",
    "05": "暴風警報",
    "06": "大雪警報",
    "07": "波浪警報",
    "08": "レベル3高潮警報",
    "09": "レベル3土砂災害警報",
    "10": "レベル2大雨注意報",
    "12": "大雪注意報",
    "13": "風雪注意報",
    "14": "雷注意報",
    "15": "強風注意報",
    "16": "波浪注意報",
    "17": "融雪注意報",
    "18": "洪水注意報",
    "19": "レベル2高潮注意報",
    "20": "濃霧注意報",
    "21": "乾燥注意報",
    "22": "なだれ注意報",
    "23": "低温注意報",
    "24": "霜注意報",
    "25": "着氷注意報",
    "26": "着雪注意報",
    "27": "その他の注意報",
    "29": "レベル2土砂災害注意報",
    "32": "暴風雪特別警報",
    "33": "レベル5大雨特別警報",
    "35": "暴風特別警報",
    "36": "大雪特別警報",
    "37": "波浪特別警報",
    "38": "レベル5高潮特別警報",
    "39": "レベル5土砂災害特別警報",
    "43": "レベル4大雨危険警報",
    "48": "レベル4高潮危険警報",
    "49": "レベル4土砂災害危険警報"
}

# ────────────────────────────────
# サンプルデータの読み込み
DATA_FILE = os.path.join(APP_DIR, 'data', 'shelters.json')
INSTRUCTIONS_FILE = os.path.join(APP_DIR, 'data', 'instructions.json')
ANNOUNCEMENTS_FILE = os.path.join(APP_DIR, 'data', 'announcements.json')

def load_json(path, default):
    """JSONファイルを読み込む（存在しない・壊れている場合は default を返す）"""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

shelters = load_json(DATA_FILE, [])
instructions = load_json(INSTRUCTIONS_FILE, [])
announcements = load_json(ANNOUNCEMENTS_FILE, [])


def save_json_file(path, data):
    """JSONを一時ファイル経由で置換し、失敗を呼び出し元へ伝える"""
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            'w', encoding='utf-8', dir=os.path.dirname(path), delete=False
        ) as file:
            temporary_path = file.name
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)


def save_instructions(records=None):
    save_json_file(INSTRUCTIONS_FILE, instructions if records is None else records)


def save_shelters():
    """避難所データをファイルに保存する"""
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(shelters, f, ensure_ascii=False, indent=2)


BOARD_TARGETS = ('住民', '防災課', '道路管理課')
BOARD_DISTRICTS = ('市内全域', '青森地区', '浪岡地区')
BOARD_URGENCIES = ('高', '中', '低')
BOARD_STATUSES = ('未対応', '対応中', '完了')
URGENCY_ORDER = {'高': 0, '中': 1, '低': 2}


def board_district_options():
    return list(dict.fromkeys((*BOARD_DISTRICTS, *shelter_districts())))


def board_shelter_options():
    return list(dict.fromkeys(
        shelter.get('name', '').strip()
        for shelter in shelters
        if isinstance(shelter, dict)
        and isinstance(shelter.get('name'), str)
        and shelter.get('name', '').strip()
    ))


def board_csrf_token():
    if 'board_csrf_token' not in session:
        session['board_csrf_token'] = secrets.token_urlsafe(32)
    return session['board_csrf_token']


def board_csrf_valid():
    submitted = request.form.get('csrf_token', '')
    expected = session.get('board_csrf_token', '')
    return bool(expected and secrets.compare_digest(submitted, expected))


def board_timestamp():
    return datetime.now(JST).isoformat(timespec='seconds')


def board_timestamp_order(value):
    if not isinstance(value, str):
        return 0
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=JST)
        return parsed.timestamp()
    except ValueError:
        match = re.fullmatch(r'(\d{4})年(\d{2})月(\d{2})日 (\d{2}):(\d{2})', value)
        if match:
            return datetime(*map(int, match.groups()), tzinfo=JST).timestamp()
        return 0


def format_board_timestamp(value):
    if not isinstance(value, str):
        return '不明'
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo:
            parsed = parsed.astimezone(JST)
        return parsed.strftime('%Y年%m月%d日 %H:%M')
    except ValueError:
        return value


app.jinja_env.filters['board_time'] = format_board_timestamp


def canonical_instruction_status(value):
    return value if value in BOARD_STATUSES else '未対応'


app.jinja_env.globals['canonical_instruction_status'] = canonical_instruction_status


def validate_announcement(data):
    errors = []
    if data['district'] not in board_district_options():
        errors.append('対象地区を選択してください。')
    if data['urgency'] not in BOARD_URGENCIES:
        errors.append('緊急度の指定が正しくありません。')
    if not data['content']:
        errors.append('発信内容を入力してください。')
    if not data['easy_content']:
        errors.append('やさしい日本語での発信内容を入力してください。')
    if data['shelter'] and data['shelter'] not in board_shelter_options():
        errors.append('選択された避難先は登録されていません。')
    return errors
# ────────────────────────────────

# ────────────────────────────────
# 認証関連の設定とヘルパー関数
def is_safe_url(target):
    """リダイレクト先URLが安全かどうかチェック"""
    ref_url = urlparse(request.host_url)
    test_url = urlparse(urljoin(request.host_url, target))
    return test_url.scheme in ('http', 'https') and ref_url.netloc == test_url.netloc

def login_required(f):
    """認証が必要なページに付けるデコレータ"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            # 現在のURLをnextパラメータとしてログイン画面にリダイレクト
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def get_japan_time():
    """日本時間（JST）の現在時刻を取得する"""
    return datetime.now(JST).strftime("%Y年%m月%d日 %H:%M")


app.jinja_env.globals['get_japan_time'] = get_japan_time


def format_report_time(iso_str):
    """気象庁の発表時刻（ISO形式）をJSTの表示用文字列に変換する"""
    if not iso_str:
        return "不明"
    try:
        parsed = datetime.fromisoformat(iso_str.replace('Z', '+00:00'))
        if parsed.tzinfo:
            parsed = parsed.astimezone(JST)
        return parsed.strftime("%Y年%m月%d日 %H:%M")
    except ValueError:
        return iso_str


DISTRICTS = ('北地区', '南地区', '西地区', '東地区')
DISASTER_TYPES = ('地震', '津波', '洪水', '土砂災害', '高潮', '大規模な火事')


def shelter_districts():
    """登録済み避難所にある地区名を重複なく返す"""
    registered_districts = {
        district.strip()
        for shelter in shelters
        if isinstance(shelter, dict)
        and isinstance((district := shelter.get('district')), str)
        and district.strip()
    }
    return list(dict.fromkeys((*DISTRICTS, *sorted(registered_districts - set(DISTRICTS)))))


def shelter_search_conditions(args):
    """検索条件を検証し、画面とAPIで共通利用できる形にする"""
    district = args.get('district', '').strip()
    keyword = args.get('keyword', '').strip()
    disaster_types = args.getlist('disaster_types')
    disaster_type = args.get('disaster_type', '').strip()
    if disaster_type:
        disaster_types.append(disaster_type)
    disaster_types = list(dict.fromkeys(disaster_types))
    pets_allowed = args.get('pets_allowed', '').strip()
    barrier_free = args.get('barrier_free', '').strip()
    errors = []

    if district and district not in shelter_districts():
        errors.append('選択された地区は登録されていません。')
    invalid_disaster_types = [value for value in disaster_types if value not in DISASTER_TYPES]
    if invalid_disaster_types:
        errors.append('災害種別の指定が正しくありません。')
    if pets_allowed not in ('', 'on', 'true', '1'):
        errors.append('ペット可の指定が正しくありません。')
    if barrier_free not in ('', 'on', 'true', '1'):
        errors.append('バリアフリーの指定が正しくありません。')

    return {
        'district': district,
        'keyword': keyword,
        'disaster_types': disaster_types,
        'pets_allowed': bool(pets_allowed),
        'barrier_free': bool(barrier_free),
    }, errors


def filter_shelters(conditions=None):
    """指定されたすべての条件に一致する避難所だけを返す"""
    conditions = conditions or {}
    results = []
    for shelter in shelters:
        if not isinstance(shelter, dict):
            continue
        if conditions.get('district'):
            registered_district = shelter.get('district')
            if not isinstance(registered_district, str) or registered_district.strip() != conditions['district']:
                continue
        keyword = conditions.get('keyword', '').casefold()
        name = shelter.get('name')
        if keyword and (not isinstance(name, str) or keyword not in name.casefold()):
            continue
        required_disasters = conditions.get('disaster_types', [])
        if required_disasters:
            registered_disasters = shelter.get('disaster_types')
            if not isinstance(registered_disasters, list) or not all(
                disaster in registered_disasters for disaster in required_disasters
            ):
                continue
        if conditions.get('pets_allowed'):
            pets_allowed = shelter.get('pets_allowed', shelter.get('pet_allowed'))
            if pets_allowed is not True:
                continue
        if conditions.get('barrier_free') and shelter.get('barrier_free') is not True:
            continue
        results.append(shelter)
    return results


def search_condition_labels(conditions):
    """検索条件を画面表示用のラベルに変換する"""
    labels = []
    if conditions.get('district'):
        labels.append(f"地区: {conditions['district']}")
    if conditions.get('keyword'):
        labels.append(f"避難所名: {conditions['keyword']}")
    if conditions.get('disaster_types'):
        labels.append(f"災害種別: {'、'.join(conditions['disaster_types'])}")
    if conditions.get('pets_allowed'):
        labels.append('ペット可')
    if conditions.get('barrier_free'):
        labels.append('バリアフリー')
    return labels


def shelter_result_details(shelter):
    """避難所の状態を検索結果表示用に整え、未登録情報は推測しない"""
    def read_count(*keys):
        for key in keys:
            value = shelter.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, int) and value >= 0:
                return value
            if isinstance(value, str) and re.fullmatch(r'\d+', value.strip()):
                return int(value.strip())
        return None

    def known_status(keys):
        for key in keys:
            value = shelter.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    status_styles = {
        '空きあり': ('空きあり', 'green', '空いている'),
        '空いている': ('空きあり', 'green', '空いている'),
        'available': ('空きあり', 'green', '空いている'),
        '空き少なめ': ('空き少なめ', 'yellow', 'やや混雑'),
        'やや混雑': ('空き少なめ', 'yellow', 'やや混雑'),
        '空き少': ('空き少なめ', 'yellow', 'やや混雑'),
        '満室に近い': ('満室に近い', 'orange', '混雑'),
        '混雑': ('満室に近い', 'orange', '混雑'),
        '満室': ('満室', 'red', '満室'),
        'full': ('満室', 'red', '満室'),
    }

    def styled_status(value, is_crowding=False):
        canonical = status_styles.get(value.casefold(), status_styles.get(value))
        if canonical:
            label = canonical[2] if is_crowding else canonical[0]
            return {'label': label, 'color': canonical[1]}
        return {'label': value, 'color': 'neutral'}

    capacity = read_count('capacity', 'capacity_count')
    evacuees = read_count(
        'current_evacuees', 'evacuee_count', 'evacuees',
        'current_occupants', 'occupants'
    )
    explicit_vacancy = known_status(('availability_status', 'vacancy_status', 'availability'))
    explicit_crowding = known_status(('crowding_status', 'congestion_status', 'occupancy_status'))
    ratio_status = None
    if capacity is not None and capacity > 0 and evacuees is not None:
        ratio = evacuees / capacity
        if ratio < 0.5:
            ratio_status = ('空きあり', 'green', '空いている')
        elif ratio < 0.75:
            ratio_status = ('空き少なめ', 'yellow', 'やや混雑')
        elif ratio < 1:
            ratio_status = ('満室に近い', 'orange', '混雑')
        else:
            ratio_status = ('満室', 'red', '満室')

    vacancy = (
        styled_status(explicit_vacancy)
        if explicit_vacancy
        else {'label': ratio_status[0], 'color': ratio_status[1]} if ratio_status
        else {'label': '状況不明', 'color': 'neutral'}
    )
    crowding = (
        styled_status(explicit_crowding, is_crowding=True)
        if explicit_crowding
        else {'label': ratio_status[2], 'color': ratio_status[1]} if ratio_status
        else {'label': '状況不明', 'color': 'neutral'}
    )
    pets_allowed = shelter.get('pets_allowed', shelter.get('pet_allowed'))
    amenities = []
    if pets_allowed is True:
        amenities.append({'label': 'ペット可', 'icon': '🐾', 'aria_label': 'ペット可の設備あり'})
    elif pets_allowed is False:
        amenities.append({'label': 'ペット不可', 'icon': None, 'aria_label': 'ペット不可'})
    if shelter.get('barrier_free') is True:
        amenities.append({'label': 'バリアフリー', 'icon': '♿', 'aria_label': 'バリアフリー設備あり'})
    elif shelter.get('barrier_free') is False:
        amenities.append({'label': 'バリアフリー非対応', 'icon': None, 'aria_label': 'バリアフリー非対応'})

    return {
        'capacity': capacity,
        'evacuees': evacuees,
        'vacancy': vacancy,
        'crowding': crowding,
        'amenities': amenities,
        'amenities_unregistered': not amenities,
    }


def parse_area_warnings(warning_data):
    """気象庁の新形式JSONから対象市区町村の発表・継続中の情報を抽出する"""
    if not isinstance(warning_data, list):
        raise ValueError("気象庁の警報・注意報データが新形式の配列ではありません")

    warnings = []
    seen_codes = set()
    report_datetimes = []

    for report in warning_data:
        if not isinstance(report, dict):
            continue

        report_datetime = report.get("reportDatetime")
        if isinstance(report_datetime, str) and report_datetime:
            report_datetimes.append(report_datetime)

        warning = report.get("warning")
        if not isinstance(warning, dict):
            continue

        class20_items = warning.get("class20Items", [])
        if not isinstance(class20_items, list):
            continue

        area = next(
            (
                item for item in class20_items
                if isinstance(item, dict)
                and item.get("areaCode") == AREA_CODE
            ),
            None
        )
        if not area:
            continue

        kinds = area.get("kinds", [])
        if not isinstance(kinds, list):
            continue

        for kind in kinds:
            if not isinstance(kind, dict):
                continue

            status = kind.get("status", "")
            code = kind.get("code", "")
            if status not in ("発表", "継続") or not code or code in seen_codes:
                continue

            warnings.append({
                "name": WARNING_CODES.get(
                    code,
                    f"不明な警報・注意報 (コード: {code})"
                ),
                "code": code,
                "status": status
            })
            seen_codes.add(code)

    latest_report_datetime = max(report_datetimes, default="")
    return warnings, latest_report_datetime


def get_weather_warnings():
    """対象市区町村の警報・注意報を取得する"""
    try:
        # 青森県の新形式（令和8年～）警報・注意報データを取得
        with urllib.request.urlopen(url=WARNING_URL, timeout=10) as res:
            warning_data = json.loads(res.read())

        warnings, report_datetime = parse_area_warnings(warning_data)

        return {
            "area_name": AREA_NAME,
            "warnings": warnings,
            "report_time": format_report_time(report_datetime),
            "last_fetch_time": get_japan_time()
        }

    except Exception:
        return {
            "area_name": AREA_NAME,
            "warnings": [],
            "report_time": "取得失敗",
            "last_fetch_time": get_japan_time(),
            "error": True
        }


# トップページ：templates/index.html を返す（住民向け指示も表示する）
@app.route('/')
def index():
    resident_notices = [i for i in instructions if i.get('target') == '住民']
    latest_announcements = sorted(
        (item for item in announcements if isinstance(item, dict)),
        key=lambda item: (board_timestamp_order(item.get('published_at')), item.get('id', 0)),
        reverse=True
    )
    return render_template(
        'index.html',
        resident_notices=resident_notices,
        announcements=latest_announcements
    )

# ログインページ
@app.route('/login', methods=['GET', 'POST'])
def login():
    # リダイレクト先を取得（デフォルトは避難所登録画面）
    next_url = request.args.get('next') or request.form.get('next')

    # 安全でないURLの場合はデフォルトページにリダイレクト
    if not next_url or not is_safe_url(next_url):
        next_url = url_for('shelter_register')

    if request.method == 'POST':
        password = request.form.get('password', '').strip()

        # 認証チェック
        username = next(
            (name for name, registered_password in ADMIN_CREDENTIALS.items()
             if registered_password == password),
            None
        )
        if username:
            session['logged_in'] = True
            session['username'] = username
            # ログイン成功後は指定されたページにリダイレクト
            return redirect(next_url)
        return render_template('login.html', error=True, message="パスワードが正しくありません。", next=next_url)

    # ログイン済みの場合は指定されたページにリダイレクト
    if session.get('logged_in'):
        return redirect(next_url)

    return render_template('login.html', next=next_url)

# ログアウト
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# 避難所登録ページ
@app.route('/shelter_register', methods=['GET', 'POST'])
@login_required
def shelter_register():
    default_form = {
        'name': '',
        'address': '',
        'capacity': '',
        'district': '',
        'disaster_types': [],
        'pets_allowed': False,
        'barrier_free': False,
        'status': '開設中'
    }

    if request.method == 'POST':
        form_data = {
            'name': request.form.get('name', '').strip(),
            'address': request.form.get('address', '').strip(),
            'capacity': request.form.get('capacity', '').strip(),
            'district': request.form.get('district', '').strip(),
            'disaster_types': request.form.getlist('disaster_types'),
            'pets_allowed': request.form.get('pets_allowed') is not None,
            'barrier_free': request.form.get('barrier_free') is not None,
            'status': request.form.get('status', '開設中')
        }

        action = request.form.get('action', 'confirm')

        if action == 'back':
            return render_template(
                'shelter_register.html',
                form_data=form_data,
                show_confirm=False,
                district_options=DISTRICTS
            )

        errors = []

        if not form_data['name']:
            errors.append('避難所名を入力してください。')
        if not form_data['address']:
            errors.append('住所を入力してください。')
        if not re.fullmatch(r'\d+', form_data['capacity']):
            errors.append('収容人数は半角数字で入力してください。')
        if form_data['district'] and form_data['district'] not in DISTRICTS:
            errors.append('地区を選択してください。')
        if any(value not in DISASTER_TYPES for value in form_data['disaster_types']):
            errors.append('災害の種類の指定が正しくありません。')
        if form_data['status'] not in ('開設中', '閉鎖中'):
            errors.append('開設状況の指定が正しくありません。')

        if action == 'submit':
            if errors:
                return render_template(
                    'shelter_register.html',
                    form_data=form_data,
                    show_confirm=False,
                    errors=errors,
                    district_options=DISTRICTS
                )

            new_id = max((s.get('id', 0) for s in shelters), default=0) + 1
            new_shelter = {
                'id': new_id,
                'name': form_data['name'],
                'address': form_data['address'],
                'capacity': int(form_data['capacity']),
                'district': form_data['district'],
                'disaster_types': form_data['disaster_types'],
                'pets_allowed': form_data['pets_allowed'],
                'barrier_free': form_data['barrier_free'],
                'status': form_data['status']
            }
            shelters.append(new_shelter)
            try:
                save_shelters()
            except OSError:
                shelters.pop()
                return render_template(
                    'shelter_register.html',
                    form_data=form_data,
                    show_confirm=False,
                    errors=['避難所情報を保存できませんでした。時間をおいて再度お試しください。'],
                    district_options=DISTRICTS
                ), 500

            return render_template(
                'shelter_register.html',
                form_data=default_form,
                success=True,
                message='登録が完了しました',
                district_options=DISTRICTS
            )

        if errors:
            return render_template(
                'shelter_register.html',
                form_data=form_data,
                show_confirm=False,
                errors=errors,
                district_options=DISTRICTS
            )

        return render_template(
            'shelter_register.html',
            form_data=form_data,
            show_confirm=True,
            district_options=DISTRICTS
        )

    return render_template(
        'shelter_register.html',
        form_data=default_form,
        district_options=DISTRICTS
    )

# 避難所検索ページ
@app.route('/shelter_search')
def shelter_search():
    conditions, errors = shelter_search_conditions(request.args)
    if errors:
        return render_template(
            'shelter_search.html',
            districts=shelter_districts(),
            disaster_types=DISASTER_TYPES,
            conditions=conditions,
            errors=errors
        ), 400
    return render_template(
        'shelter_search.html',
        districts=shelter_districts(),
        disaster_types=DISASTER_TYPES,
        conditions=conditions
    )

# 全施設一覧ページ
@app.route('/all_shelters')
def all_shelters():
    return render_template(
        'search_results.html',
        results=[
            {**shelter, '_result_details': shelter_result_details(shelter)}
            for shelter in shelters if isinstance(shelter, dict)
        ],
        full_list=True,
        condition_labels=[],
        search_url=url_for('shelter_search')
    )


@app.route('/shelter/<int:shelter_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_shelter(shelter_id):
    shelter = next(
        (item for item in shelters if isinstance(item, dict) and item.get('id') == shelter_id),
        None
    )
    if shelter is None:
        return '避難所が見つかりません。', 404

    district_options = shelter_districts()
    form_data = {
        'name': shelter.get('name', ''),
        'address': shelter.get('address', ''),
        'capacity': str(shelter.get('capacity', '')),
        'district': shelter.get('district', ''),
        'disaster_types': shelter.get('disaster_types', []) if isinstance(shelter.get('disaster_types'), list) else [],
        'status': shelter.get('status', '開設中')
    }
    errors = []

    if request.method == 'POST':
        form_data = {
            'name': request.form.get('name', '').strip(),
            'address': request.form.get('address', '').strip(),
            'capacity': request.form.get('capacity', '').strip(),
            'district': request.form.get('district', '').strip(),
            'disaster_types': request.form.getlist('disaster_types'),
            'status': request.form.get('status', '開設中')
        }
        if not form_data['name']:
            errors.append('避難所名を入力してください。')
        if not form_data['address']:
            errors.append('住所を入力してください。')
        if not re.fullmatch(r'\d+', form_data['capacity']):
            errors.append('収容人数は半角数字で入力してください。')
        if form_data['district'] and form_data['district'] not in district_options:
            errors.append('地区を選択してください。')
        if any(value not in DISASTER_TYPES for value in form_data['disaster_types']):
            errors.append('災害の種類の指定が正しくありません。')
        if form_data['status'] not in ('開設中', '閉鎖中'):
            errors.append('開設状況の指定が正しくありません。')

        if not errors:
            old_values = dict(shelter)
            shelter.update({
                'name': form_data['name'],
                'address': form_data['address'],
                'capacity': int(form_data['capacity']),
                'district': form_data['district'],
                'disaster_types': form_data['disaster_types'],
                'status': form_data['status']
            })
            try:
                save_shelters()
            except OSError:
                shelter.clear()
                shelter.update(old_values)
                errors.append('避難所情報を保存できませんでした。時間をおいて再度お試しください。')
            else:
                return redirect(url_for('all_shelters'))

    return render_template(
        'shelter_edit.html',
        shelter=shelter,
        form_data=form_data,
        errors=errors,
        disaster_types=DISASTER_TYPES,
        district_options=district_options
    )


# 指示・発信ボード
@app.route('/board', methods=['GET', 'POST'])
@login_required
def board():
    district_options = board_district_options()
    shelter_options = board_shelter_options()
    sort_order = request.values.get('sort', 'newest')
    if sort_order not in ('newest', 'urgency'):
        sort_order = 'newest'

    form_data = session.pop('instruction_form', None) or {
        'target': '住民',
        'district': '市内全域',
        'urgency': '中',
        'content': '',
        'shelter': '',
    }
    announcement_data = session.pop('announcement_form', None) or {
        'district': '市内全域',
        'urgency': '中',
        'content': '',
        'easy_content': '',
        'shelter': '',
    }
    announcement_history = sorted(
        (item for item in announcements if isinstance(item, dict)),
        key=lambda item: (board_timestamp_order(item.get('published_at')), item.get('id', 0)),
        reverse=True
    )
    errors = []

    if request.method == 'POST':
        if not board_csrf_valid():
            return 'リクエストを確認できませんでした。ページを再読み込みしてください。', 400

        action = request.form.get('action', '')
        if action == 'register':
            form_data = {
                'target': request.form.get('target', '').strip(),
                'district': request.form.get('district', '').strip(),
                'urgency': request.form.get('urgency', '').strip(),
                'content': request.form.get('content', '').strip(),
                'shelter': request.form.get('shelter', '').strip(),
            }
            if form_data['target'] not in BOARD_TARGETS:
                errors.append('宛先を選択してください。')
            if form_data['district'] not in district_options:
                errors.append('対象地区を選択してください。')
            if form_data['urgency'] not in BOARD_URGENCIES:
                errors.append('緊急度の指定が正しくありません。')
            if not form_data['content']:
                errors.append('指示内容を入力してください。')
            if form_data['shelter'] and form_data['shelter'] not in shelter_options:
                errors.append('選択された避難先は登録されていません。')

            if not errors:
                next_id = max(
                    (item.get('id', 0) for item in instructions
                     if isinstance(item, dict) and isinstance(item.get('id'), int)),
                    default=0
                ) + 1
                now = board_timestamp()
                record = {
                    'id': next_id,
                    **form_data,
                    'status': '未対応',
                    'created_at': now,
                    'updated_at': now,
                }
                candidate = [*instructions, record]
                try:
                    save_instructions(candidate)
                except OSError:
                    errors.append('指示を保存できませんでした。入力内容を保ったまま再度お試しください。')
                    session['instruction_form'] = form_data
                    return render_template(
                        'board.html', instructions=instructions, form_data=form_data,
                        announcement_data=announcement_data, announcements=announcement_history, errors=errors,
                        district_options=district_options, shelter_options=shelter_options,
                        targets=BOARD_TARGETS, urgencies=BOARD_URGENCIES,
                        statuses=BOARD_STATUSES, sort_order=sort_order,
                        csrf_token=board_csrf_token()
                    ), 500
                instructions[:] = candidate
                flash('指示を登録しました。', 'success')
                return redirect(url_for('board', sort=sort_order))

        elif action == 'update_status':
            instruction_id = request.form.get('instruction_id', '')
            status = request.form.get('status', '')
            if not instruction_id.isdigit() or status not in BOARD_STATUSES:
                flash('状態の指定が正しくありません。', 'error')
                return redirect(url_for('board', sort=sort_order))
            candidate = [dict(item) if isinstance(item, dict) else item for item in instructions]
            record = next(
                (item for item in candidate if isinstance(item, dict)
                 and str(item.get('id')) == instruction_id),
                None
            )
            if record is None:
                flash('更新する指示が見つかりません。', 'error')
                return redirect(url_for('board', sort=sort_order))
            record['status'] = status
            record['updated_at'] = board_timestamp()
            try:
                save_instructions(candidate)
            except OSError:
                flash('状態を保存できませんでした。時間をおいて再度お試しください。', 'error')
            else:
                instructions[:] = candidate
                flash('対応状況を更新しました。', 'success')
            return redirect(url_for('board', sort=sort_order))

        elif action == 'review_announcement':
            announcement_data = {
                'district': request.form.get('district', '').strip(),
                'urgency': request.form.get('urgency', '').strip(),
                'content': request.form.get('content', '').strip(),
                'easy_content': request.form.get('easy_content', '').strip(),
                'shelter': request.form.get('shelter', '').strip(),
            }
            errors = validate_announcement(announcement_data)
            if not errors:
                session['pending_announcement'] = announcement_data
                return redirect(url_for('confirm_announcement'))
        else:
            return '操作の指定が正しくありません。', 400

    ordered_instructions = [item for item in instructions if isinstance(item, dict)]
    if sort_order == 'urgency':
        ordered_instructions.sort(
            key=lambda item: (
                URGENCY_ORDER.get(item.get('urgency'), 3),
                -board_timestamp_order(item.get('created_at')),
                -int(item.get('id', 0)) if str(item.get('id', 0)).isdigit() else 0,
            )
        )
    else:
        ordered_instructions.sort(
            key=lambda item: (
                board_timestamp_order(item.get('created_at')),
                int(item.get('id', 0)) if str(item.get('id', 0)).isdigit() else 0,
            ),
            reverse=True
        )
    return render_template(
        'board.html', instructions=ordered_instructions, form_data=form_data,
        announcement_data=announcement_data, announcements=announcement_history, errors=errors,
        district_options=district_options, shelter_options=shelter_options,
        targets=BOARD_TARGETS, urgencies=BOARD_URGENCIES,
        statuses=BOARD_STATUSES, sort_order=sort_order,
        csrf_token=board_csrf_token()
    ), (400 if errors else 200)


@app.route('/board/confirm', methods=['GET', 'POST'])
@login_required
def confirm_announcement():
    draft = session.get('pending_announcement')
    if not isinstance(draft, dict):
        flash('確認する発信内容がありません。', 'error')
        return redirect(url_for('board'))

    errors = []
    if request.method == 'POST':
        if not board_csrf_valid():
            return 'リクエストを確認できませんでした。ページを再読み込みしてください。', 400
        action = request.form.get('action', '')
        if action == 'back':
            session['announcement_form'] = draft
            session.pop('pending_announcement', None)
            return redirect(url_for('board'))
        if action != 'publish':
            return '操作の指定が正しくありません。', 400

        errors = validate_announcement(draft)
        if errors:
            return render_template(
                'board_confirm.html', draft=draft, errors=errors,
                csrf_token=board_csrf_token()
            ), 400

        next_id = max(
            (item.get('id', 0) for item in announcements
             if isinstance(item, dict) and isinstance(item.get('id'), int)),
            default=0
        ) + 1
        record = {'id': next_id, **draft, 'published_at': board_timestamp()}
        candidate = [record, *announcements]
        try:
            save_json_file(ANNOUNCEMENTS_FILE, candidate)
        except OSError:
            return render_template(
                'board_confirm.html', draft=draft,
                errors=['お知らせを保存できませんでした。発信は完了していません。再度お試しください。'],
                csrf_token=board_csrf_token()
            ), 500
        announcements[:] = candidate
        session.pop('pending_announcement', None)
        flash('お知らせを発信しました。', 'success')
        return redirect(url_for('board'))

    return render_template(
        'board_confirm.html', draft=draft, errors=errors,
        csrf_token=board_csrf_token()
    )

# 検索結果ページ：templates/search_results.html を返す
@app.route('/search_results')
def search_results():
    conditions, errors = shelter_search_conditions(request.args)
    if errors:
        return render_template(
            'shelter_search.html',
            districts=shelter_districts(),
            disaster_types=DISASTER_TYPES,
            conditions=conditions,
            errors=errors
        ), 400
    results = filter_shelters(conditions)
    return render_template(
        'search_results.html',
        results=[
            {**shelter, '_result_details': shelter_result_details(shelter)}
            for shelter in results
        ],
        full_list=False,
        conditions=conditions,
        condition_labels=search_condition_labels(conditions),
        search_url=url_for('shelter_search') + (
            '?' + urlencode(list(request.args.items(multi=True))) if request.args else ''
        ),
        missing_info_filter=bool(
            conditions['disaster_types'] or conditions['pets_allowed'] or conditions['barrier_free']
        )
    )

# JSON API：/shelters?district=地区名
@app.route('/shelters', methods=['GET'])
def get_shelters():
    conditions, errors = shelter_search_conditions(request.args)
    if errors:
        return jsonify({'error': 'Invalid search conditions', 'details': errors}), 400
    return jsonify(filter_shelters(conditions))

# 気象警報・注意報API
@app.route('/api/weather_warnings')
def api_weather_warnings():
    """気象警報・注意報をJSON形式で返すAPI"""
    return jsonify(get_weather_warnings())

if __name__ == '__main__':
    app.run(debug=True, port=5000)
