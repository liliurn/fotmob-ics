import requests
import os
import re
from datetime import datetime, timedelta, timezone
from ics import Calendar, Event
import pytz # タイムゾーン処理のため
from bs4 import BeautifulSoup # HTML解析用
import json # HTML内のJSONデータをパースするため

# --- 設定 ---
FOTMOB_URLS_STR = os.environ.get("FOTMOB_URLS", "")
OUTPUT_ICS_FILE = "fotmob_schedule.ics"
DEFAULT_EVENT_DURATION_MINUTES = 105
FETCH_FUTURE_DAYS = 90
# --- 設定ここまで ---

# ユーザーエージェントを設定 (ブロック回避のため)
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9,ja;q=0.8' # 言語設定 (英語の方が構造が安定している可能性)
}

def get_entity_info_from_url(url):
    """Fotmob URLからタイプとID、およびページタイプを抽出"""
    # チームURL: https://www.fotmob.com/teams/8456/overview/manchester-city
    # リーグURL: https://www.fotmob.com/leagues/47/overview/premier-league
    # 試合詳細URL: https://www.fotmob.com/match/4219679/matchfacts/manchester-city-vs-west-ham-united
    team_match = re.search(r'/teams/(\d+)', url)
    if team_match:
        return "team", team_match.group(1)

    league_match = re.search(r'/leagues/(\d+)', url)
    if league_match:
        return "league", league_match.group(1)

    return None, None

def parse_match_data_from_script(script_content):
    """<script id="__NEXT_DATA__"> から試合データを抽出"""
    try:
        data = json.loads(script_content)
        # 構造を辿って試合データを探す (この構造は変わりやすい)
        # 例: props -> pageProps -> fixturesData -> fixtures or allFixtures
        fixtures_data = data.get('props', {}).get('pageProps', {}).get('fixturesData', {})
        if 'fixtures' in fixtures_data:
            return fixtures_data['fixtures']
        elif 'allFixtures' in fixtures_data:
            return fixtures_data['allFixtures']
        # リーグページの場合の構造 (異なる可能性がある)
        elif 'matches' in fixtures_data:
             return fixtures_data['matches'].get('allMatches') # さらにネストされている場合

        # 他の可能性のあるパスを探す (デバッグ用)
        # print("Could not find fixtures in standard paths. Checking props -> pageProps...")
        # print(data.get('props', {}).get('pageProps', {}).keys())


    except json.JSONDecodeError as e:
        print(f"Error decoding JSON from script tag: {e}")
    except Exception as e:
        print(f"Error parsing script data: {e}")
    return None


def fetch_fixtures_from_html(url, entity_type, entity_id):
    """FotmobのHTMLページから試合日程をスクレイピング"""
    print(f"Fetching HTML for {entity_type} ID {entity_id} from {url}")
    try:
        response = requests.get(url, headers=HEADERS, timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'lxml') # lxmlパーサーを使用

        # FotmobはNext.jsを使用しており、データが<script id="__NEXT_DATA__">に含まれていることが多い
        script_tag = soup.find('script', id='__NEXT_DATA__')
        if not script_tag:
            print(f"Error: Could not find <script id='__NEXT_DATA__'> tag on {url}")
            # 代替: HTML要素から直接抽出するロジック (より複雑で不安定)
            # return parse_fixtures_from_elements(soup, entity_type)
            return None

        fixtures = parse_match_data_from_script(script_tag.string)

        if fixtures:
            print(f"Successfully extracted {len(fixtures)} potential matches from script tag.")
            # APIレスポンスとキー名が異なる場合があるので、ここで変換が必要になる可能性がある
            # 例: 'startTimeEpoch' が 'kickOffTimestamp' になっているなど
            # 現在の create_ics_event はAPIレスポンス形式を期待しているので、
            # 必要ならここでキー名を合わせるか、create_ics_event を修正する。
            # ここでは、キー名がある程度似ていると仮定して進める。
            return fixtures
        else:
            print(f"Could not extract fixture data from the script tag for {url}.")
            return None

    except requests.exceptions.RequestException as e:
        print(f"Error fetching HTML for {entity_type} ID {entity_id}: {e}")
    except Exception as e:
        print(f"An unexpected error occurred while scraping {url}: {e}")
    return None

# --- create_ics_event 関数は基本的に変更なし ---
# ただし、HTMLから取得したデータのキー名がAPIと異なる場合は、
# この関数内で対応するか、fetch_fixtures_from_htmlで変換する必要がある。
# 特に日時のキー名 ('status.utcTime', 'startTimeEpoch') は要注意。
def create_ics_event(match_data):
    """Fotmobの試合データからics.Eventオブジェクトを作成"""
    try:
        # HTMLから取得したデータ構造に合わせてキー名を調整する必要があるかもしれない
        home_team_data = match_data.get('home', {})
        away_team_data = match_data.get('away', {})
        home_team = home_team_data.get('name', 'Unknown')
        away_team = away_team_data.get('name', 'Unknown')
        match_id = match_data.get('id', None)
        page_url = match_data.get('pageUrl', None) # 詳細URL

        # 試合開始時刻 (UTC) - HTMLデータ内のキーを探す
        kickoff_utc = None
        status = match_data.get('status', {})

        # 1. startTimeEpoch (Unixタイムスタンプ 秒) を試す
        if 'startTimeEpoch' in match_data:
            kickoff_epoch = match_data['startTimeEpoch']
            if isinstance(kickoff_epoch, (int, float)):
                 kickoff_utc = datetime.fromtimestamp(kickoff_epoch, tz=timezone.utc)
        # 2. status -> utcTime (ISO形式文字列) を試す
        elif 'utcTime' in status:
            kickoff_utc_str = status['utcTime']
            if isinstance(kickoff_utc_str, str) and kickoff_utc_str.endswith('Z'):
                try:
                    kickoff_utc = datetime.fromisoformat(kickoff_utc_str.replace('Z', '+00:00'))
                except ValueError:
                    print(f"警告: 試合ID {match_id} の日付形式が無効(utcTime): {kickoff_utc_str}")
        # 3. status -> startDate (ISO形式文字列) を試す
        elif 'startDate' in status:
            start_date_str = status['startDate']
            if isinstance(start_date_str, str) and start_date_str.endswith('Z'):
                try:
                    kickoff_utc = datetime.fromisoformat(start_date_str.replace('Z', '+00:00'))
                except ValueError:
                    print(f"警告: 試合ID {match_id} の日付形式が無効(startDate): {start_date_str}")

        # 4. 他の可能性のあるキー (例: 'kickOffTimestamp') - 必要に応じて追加

        if kickoff_utc is None:
            print(f"警告: 試合ID {match_id} ({home_team} vs {away_team}) の開始時刻が見つかりません。スキップします。")
            # print(f"Debug match_data keys: {match_data.keys()}")
            # if 'status' in match_data: print(f"Debug status keys: {match_data['status'].keys()}")
            return None

        # 未来の試合のみを対象とするかチェック
        now_utc = datetime.now(timezone.utc)
        if kickoff_utc < now_utc:
             # print(f"Skipping past match: {home_team} vs {away_team} ({kickoff_utc})")
             return None # 過去の試合はスキップ

        if FETCH_FUTURE_DAYS is not None:
            if kickoff_utc > now_utc + timedelta(days=FETCH_FUTURE_DAYS):
                # print(f"Skipping future match beyond limit: {home_team} vs {away_team} ({kickoff_utc})")
                return None # 指定日数より未来の試合はスキップ

        dt_end_utc = kickoff_utc + timedelta(minutes=DEFAULT_EVENT_DURATION_MINUTES)

        event = Event()
        event.name = f"{home_team} vs {away_team}"
        event.begin = kickoff_utc
        event.end = dt_end_utc
        # UIDは一意である必要がある。match_idがなければ生成する
        if match_id:
             event.uid = f"fotmob-{match_id}@yourdomain.com"
        else:
             # match_idがない場合、チーム名と開始時間から生成 (衝突の可能性あり)
             ts = int(kickoff_utc.timestamp())
             event.uid = f"fotmob-{home_team}-{away_team}-{ts}@yourdomain.com".lower().replace(" ", "-")

        event.created = datetime.now(timezone.utc)

        description_parts = []
        # リーグ/大会名
        league_name = match_data.get('leagueName')
        # HTMLからのデータでは 'tournament' -> 'name' にある場合も
        if not league_name:
             league_name = match_data.get('tournament', {}).get('name')
        if league_name:
            description_parts.append(f"リーグ: {league_name}")

        # Fotmobの試合ページへのリンク
        if page_url:
             full_url = f"https://www.fotmob.com{page_url}" if page_url.startswith('/') else page_url
             description_parts.append(f"詳細: {full_url}")

        if description_parts:
            event.description = "\n".join(description_parts)

        print(f"  作成: {event.name} ({event.begin})")
        return event

    except Exception as e:
        # エラー発生時のデバッグ情報追加
        match_id_debug = match_data.get('id', 'N/A')
        print(f"Error creating ICS event for match data (ID: {match_id_debug}): {e}")
        # print(f"Problematic match_data: {match_data}") # 必要なら詳細を出力
        return None


def main():
    """メイン処理"""
    if not FOTMOB_URLS_STR:
        print("エラー: 環境変数 'FOTMOB_URLS' が設定されていません。")
        print("例: FOTMOB_URLS=\"https://www.fotmob.com/teams/ID1/overview/...,https://www.fotmob.com/leagues/ID2/overview/...\"")
        return

    urls = [url.strip() for url in FOTMOB_URLS_STR.split(',') if url.strip()]
    all_events = []
    processed_match_ids = set() # 重複イベント防止用 (IDがない場合もあるので注意)

    for url in urls:
        # URLからタイプとIDを取得 (概要ページを期待)
        entity_type, entity_id = get_entity_info_from_url(url)
        if not entity_type or not entity_id:
            print(f"警告: URLからタイプとIDを抽出できませんでした: {url}")
            continue

        # overviewページから日程を取得
        # FotmobのURL構造に合わせて調整 (例: /fixtures をつけるなど必要なら)
        # 現在は overview URL をそのまま使う想定
        fixtures_url = url # or f"https://www.fotmob.com/{entity_type}/{entity_id}/fixtures" など構造による
        fixtures = fetch_fixtures_from_html(fixtures_url, entity_type, entity_id)

        if fixtures:
            for match in fixtures:
                event = create_ics_event(match)
                if event:
                    # IDがある場合はIDで重複チェック、ない場合はUIDでチェック
                    match_id = match.get('id')
                    event_uid = event.uid
                    if match_id:
                        if match_id not in processed_match_ids:
                            all_events.append(event)
                            processed_match_ids.add(match_id)
                    elif event_uid not in processed_match_ids:
                         all_events.append(event)
                         processed_match_ids.add(event_uid) # IDがない場合は生成したUIDで管理

    if not all_events:
        print("処理対象の試合が見つかりませんでした。ICSファイルは更新されません。")
        # 空でもファイルを作成したい場合は以下のコメントを外す
        # cal = Calendar()
        # cal.creator = "Fotmob ICS Generator (HTML Scraper)"
        # with open(OUTPUT_ICS_FILE, 'w', encoding='utf-8') as f:
        #     f.writelines(cal.serialize_iter())
        # print(f"空のICSファイル '{OUTPUT_ICS_FILE}' を作成しました。")
        return

    # カレンダーを作成しイベントを追加
    cal = Calendar()
    cal.creator = "Fotmob ICS Generator (HTML Scraper) - github.com/your-repo" # 識別子
    cal.events.update(all_events)

    # ICSファイルに書き込み
    try:
        with open(OUTPUT_ICS_FILE, 'w', encoding='utf-8') as f:
            f.writelines(cal.serialize_iter())
        print(f"ICSファイル '{OUTPUT_ICS_FILE}' が正常に生成/更新されました。({len(all_events)}件のイベント)")
    except Exception as e:
        print(f"ICSファイル '{OUTPUT_ICS_FILE}' の書き込み中にエラーが発生しました: {e}")

if __name__ == "__main__":
    main()
