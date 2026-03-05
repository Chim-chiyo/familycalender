import os
import json
import re
import base64
import requests
import psycopg2
from datetime import datetime, timedelta
from flask import Flask, request, abort, jsonify
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, TextSendMessage

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", ""))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET", ""))

# 一時的な候補保存（メモリ）
pending_events = {}  # user_id -> [events]

def get_db():
    return psycopg2.connect(os.environ.get("DATABASE_URL"), sslmode='require')

def load_events():
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id, date, title, source, created_at FROM events ORDER BY date")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return [{"id": r[0], "date": r[1], "title": r[2], "source": r[3] or "manual", "created_at": r[4]} for r in rows]
    except Exception as e:
        print(f"DB load error: {e}")
        return []

def save_event(event):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO events (id, date, title, source, created_at) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING",
            (event["id"], event["date"], event["title"], event.get("source", "manual"), event.get("created_at", ""))
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"DB save error: {e}")

def delete_event_db(event_id):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM events WHERE id = %s", (event_id,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"DB delete error: {e}")

def ocr_image(image_content):
    api_key = os.environ.get("GOOGLE_VISION_API_KEY", "")
    url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
    payload = {
        "requests": [{
            "image": {"content": base64.b64encode(image_content).decode("utf-8")},
            "features": [{"type": "TEXT_DETECTION"}]
        }]
    }
    res = requests.post(url, json=payload)
    result = res.json()
    try:
        return result["responses"][0]["fullTextAnnotation"]["text"]
    except:
        return None

def extract_nencho_events(text):
    events = []
    lines = text.split("\n")
    current_month = None
    current_date = None
    current_year = datetime.now().year
    nencho_keywords = ["年長", "5歳", "ひまわり", "ゆり", "さくら", "全園", "全学年", "全クラス", "全員"]

    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue

        # 「●4月の予定」「5月の予定」など月見出しを検出
        month_header = re.search(r'(\d{1,2})月の?予定', line)
        if month_header:
            current_month = int(month_header.group(1))
            continue

        # 月/日パターン（例：4/8、4月8日）
        full_date = re.search(r'(\d{1,2})[月/／](\d{1,2})', line)
        if full_date:
            month, day = int(full_date.group(1)), int(full_date.group(2))
            try:
                d = datetime(current_year, month, day)
                if d < datetime.now() - timedelta(days=1):
                    d = datetime(current_year + 1, month, day)
                current_date = d
                current_month = month
            except:
                pass

        # 日だけのパターン（例：「8日」「○8日」）→ current_monthと組み合わせ
        elif current_month:
            day_only = re.search(r'[○◯〇Ｏ・\s]?(\d{1,2})日', line)
            if day_only:
                day = int(day_only.group(1))
                try:
                    d = datetime(current_year, current_month, day)
                    if d < datetime.now() - timedelta(days=1):
                        d = datetime(current_year + 1, current_month, day)
                    current_date = d
                except:
                    pass

        if current_date is None:
            continue

        has_nencho = any(kw in line for kw in nencho_keywords)
        has_other_grade = any(kw in line for kw in ["年少", "年中", "3歳", "4歳"])
        no_grade_specified = not re.search(r'年少|年中|年長|3歳|4歳|5歳', line)

        if has_nencho or (no_grade_specified and not has_other_grade and current_date):
            title = re.sub(r'\d{1,2}[月/／]\d{1,2}日?', '', line)
            title = re.sub(r'\d{1,2}日', '', title)
            title = re.sub(r'年長|全園|全学年|全クラス|全員|5歳|（.*?）|\(.*?\)', '', title)
            title = re.sub(r'[●◎○◯〇・\-\s　※]+', ' ', title).strip()
            if len(title) < 2:
                continue
            events.append({
                "id": str(int(datetime.now().timestamp() * 1000)) + str(len(events)),
                "date": current_date.strftime("%Y-%m-%d"),
                "title": title,
                "created_at": datetime.now().isoformat(),
                "source": "preschool"
            })
    seen = set()
    unique = []
    for e in events:
        key = e["date"] + e["title"]
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique

def parse_event(text):
    patterns = [
        r'(\d{1,2})[/／](\d{1,2})',
        r'(\d{1,2})月(\d{1,2})日',
    ]
    month, day = None, None
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            month, day = int(match.group(1)), int(match.group(2))
            break
    if not month:
        return None
    year = datetime.now().year
    try:
        event_date = datetime(year, month, day)
        if event_date < datetime.now() - timedelta(days=1):
            event_date = datetime(year + 1, month, day)
    except:
        return None
    name = re.sub(r'\d{1,2}[/／月]\d{1,2}日?', '', text).strip()
    name = re.sub(r'[がはにでのあるだよね！!。、]', '', name).strip()
    if not name:
        name = "予定"
    return {
        "id": str(int(datetime.now().timestamp() * 1000)),
        "date": event_date.strftime("%Y-%m-%d"),
        "title": name,
        "created_at": datetime.now().isoformat()
    }

def notify_other_user(sender_id, message):
    user1 = os.environ.get("USER_ID_1", "")
    user2 = os.environ.get("USER_ID_2", "")
    target = user2 if sender_id == user1 else user1
    if target:
        try:
            line_bot_api.push_message(target, TextSendMessage(text=message))
        except:
            pass

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=ImageMessage)
def handle_image(event):
    sender_id = event.source.user_id
    message_content = line_bot_api.get_message_content(event.message.id)
    image_data = b""
    for chunk in message_content.iter_content():
        image_data += chunk
    text = ocr_image(image_data)
    if not text:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="📷 画像を読み取れませんでした。明るい場所で撮り直してください。"))
        return
    extracted = extract_nencho_events(text)
    if not extracted:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="📋 年長・全学年の予定が見つかりませんでした。プリント全体が写るように撮ってください。"))
        return

    # 候補を一時保存
    pending_events[sender_id] = extracted

    # 候補一覧を返信
    lines = ["📋 以下の予定が見つかりました！\n登録したい番号を送ってください\n（例：1,3,5 または 全部）\n"]
    for i, e in enumerate(extracted[:15], 1):
        date_str = datetime.strptime(e["date"], "%Y-%m-%d").strftime("%m月%d日")
        lines.append(f"{i}. {date_str}「{e['title']}」")
    lines.append("\n✏️ 修正したい場合は「2. 3月15日 参観日」のように送ってください")
    reply = "\n".join(lines)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text
    sender_id = event.source.user_id

    # 候補選択モード
    if sender_id in pending_events:
        candidates = pending_events[sender_id]

        # 修正コマンド「2. 3月15日 参観日」
        edit_match = re.match(r'^(\d+)[.\s。]+(\d{1,2})月(\d{1,2})日?\s*(.+)$', text.strip())
        if edit_match:
            idx = int(edit_match.group(1)) - 1
            month, day, new_title = int(edit_match.group(2)), int(edit_match.group(3)), edit_match.group(4).strip()
            if 0 <= idx < len(candidates):
                year = datetime.now().year
                try:
                    new_date = datetime(year, month, day)
                    if new_date < datetime.now() - timedelta(days=1):
                        new_date = datetime(year + 1, month, day)
                    candidates[idx]["date"] = new_date.strftime("%Y-%m-%d")
                    candidates[idx]["title"] = new_title
                    pending_events[sender_id] = candidates
                    date_str = new_date.strftime("%m月%d日")
                    reply = f"✏️ {idx+1}番を「{date_str}「{new_title}」」に修正しました！\n\n他に修正があれば送ってください。\n登録する番号を送ってください（例：1,3,5 または 全部）"
                    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
                    return
                except:
                    pass

        # 全部登録
        if text.strip() in ["全部", "すべて", "全て", "all"]:
            selected = candidates
        else:
            # 番号選択「1,3,5」
            nums = re.findall(r'\d+', text)
            selected = [candidates[int(n)-1] for n in nums if 0 < int(n) <= len(candidates)]

        if selected:
            for e in selected:
                save_event(e)
            del pending_events[sender_id]
            summary = "\n".join([
                f"📅 {datetime.strptime(e['date'], '%Y-%m-%d').strftime('%m月%d日')}「{e['title']}」"
                for e in selected
            ])
            reply = f"✅ {len(selected)}件を登録しました！\n\n{summary}\n\nカレンダー👇\n" + os.environ.get("APP_URL", "") + "/calendar"
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
            notify_other_user(sender_id, f"📅 {len(selected)}件の予定が追加されました！\n\n{summary}\n\nカレンダー👇\n" + os.environ.get("APP_URL", "") + "/calendar")
        else:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text="番号が見つかりませんでした。\n例：1,3,5 または 全部"))
        return

    if text.lower() in ["myid", "userid"]:
        reply = f"あなたのユーザーIDは👇\n{sender_id}\n\nこのIDをRenderの環境変数に設定してください！"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    if text in ["ヘルプ", "help", "使い方"]:
        reply = "📅 使い方\n\n「3/4 飲み会」のように日付＋予定を送ると自動登録します！\n\nカレンダー確認はこちら👇\n" + os.environ.get("APP_URL", "") + "/calendar"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    parsed = parse_event(text)
    if parsed:
        save_event(parsed)
        date_str = datetime.strptime(parsed["date"], "%Y-%m-%d").strftime("%m月%d日")
        reply = f"✅ 登録しました！\n📅 {date_str}「{parsed['title']}」\n\nカレンダー👇\n" + os.environ.get("APP_URL", "") + "/calendar"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        notify_other_user(sender_id, f"📅 新しい予定が追加されました！\n{date_str}「{parsed['title']}」\n\nカレンダー👇\n" + os.environ.get("APP_URL", "") + "/calendar")

@app.route("/calendar")
def calendar():
    events = load_events()
    events_json = json.dumps(events, ensure_ascii=False)
    html = """
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>家族カレンダー</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #f5f5f5; color: #333; }
  header { background: #4CAF50; color: white; padding: 16px; text-align: center; }
  header h1 { font-size: 20px; }
  .nav { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px; background: white; border-bottom: 1px solid #eee; }
  .nav button { background: none; border: none; font-size: 22px; cursor: pointer; color: #4CAF50; padding: 4px 12px; }
  .nav .month-label { font-size: 18px; font-weight: bold; }
  .calendar { background: white; margin: 12px; border-radius: 12px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
  .weekdays { display: grid; grid-template-columns: repeat(7, 1fr); background: #f9f9f9; }
  .weekday { text-align: center; padding: 8px 0; font-size: 12px; color: #999; }
  .weekday:first-child { color: #e53935; }
  .weekday:last-child { color: #1565c0; }
  .days { display: grid; grid-template-columns: repeat(7, 1fr); }
  .day { min-height: 64px; padding: 4px; border-top: 1px solid #f0f0f0; border-right: 1px solid #f0f0f0; position: relative; overflow: hidden; }
  .day:nth-child(7n) { border-right: none; }
  .day-num { font-size: 13px; font-weight: 500; margin-bottom: 2px; }
  .day.sunday .day-num { color: #e53935; }
  .day.saturday .day-num { color: #1565c0; }
  .day.today .day-num { background: #4CAF50; color: white; border-radius: 50%; width: 24px; height: 24px; display: flex; align-items: center; justify-content: center; }
  .day.other-month { background: #fafafa; }
  .day.other-month .day-num { color: #ccc; }
  .event { background: #E8F5E9; color: #2E7D32; font-size: 9px; border-radius: 3px; padding: 1px 3px; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; cursor: pointer; max-width: 100%; display: block; }
  .event:hover { background: #C8E6C9; }
  .event-list { margin: 12px; }
  .event-list h2 { font-size: 16px; margin-bottom: 8px; color: #555; }
  .event-item { background: white; border-radius: 10px; padding: 12px 16px; margin-bottom: 8px; display: flex; justify-content: space-between; align-items: center; box-shadow: 0 1px 4px rgba(0,0,0,0.06); }
  .event-item .info .date { font-size: 12px; color: #999; }
  .event-item .info .title { font-size: 15px; font-weight: 500; margin-top: 2px; }
  .delete-btn { background: none; border: none; color: #ccc; font-size: 18px; cursor: pointer; padding: 4px; }
  .delete-btn:hover { color: #e53935; }
  .empty { text-align: center; color: #bbb; padding: 24px; font-size: 14px; }
</style>
</head>
<body>
<header><h1>🏠 家族カレンダー</h1></header>
<div class="nav">
  <button onclick="changeMonth(-1)">‹</button>
  <span class="month-label" id="monthLabel"></span>
  <button onclick="changeMonth(1)">›</button>
</div>
<div class="calendar">
  <div class="weekdays">
    <div class="weekday">日</div><div class="weekday">月</div>
    <div class="weekday">火</div><div class="weekday">水</div>
    <div class="weekday">木</div><div class="weekday">金</div>
    <div class="weekday">土</div>
  </div>
  <div class="days" id="calendarDays"></div>
</div>
<div class="event-list">
  <h2>📋 今月の予定</h2>
  <div id="eventList"></div>
</div>
<script>
const allEvents = """ + events_json + """;
let currentYear = new Date().getFullYear();
let currentMonth = new Date().getMonth();
function changeMonth(delta) {
  currentMonth += delta;
  if (currentMonth > 11) { currentMonth = 0; currentYear++; }
  if (currentMonth < 0) { currentMonth = 11; currentYear--; }
  render();
}
function deleteEvent(id) {
  fetch('/delete/' + id, { method: 'POST' }).then(() => location.reload());
}
function render() {
  const now = new Date();
  const today = new Date(now.toLocaleString('ja-JP', {timeZone: 'Asia/Tokyo'}));
  document.getElementById('monthLabel').textContent = currentYear + '年' + (currentMonth + 1) + '月';
  const firstDay = new Date(currentYear, currentMonth, 1).getDay();
  const daysInMonth = new Date(currentYear, currentMonth + 1, 0).getDate();
  const daysInPrev = new Date(currentYear, currentMonth, 0).getDate();
  const monthEvents = {};
  allEvents.forEach(e => {
    const d = new Date(e.date + 'T00:00:00+09:00');
    if (d.getFullYear() === currentYear && d.getMonth() === currentMonth) {
      const key = d.getDate();
      if (!monthEvents[key]) monthEvents[key] = [];
      monthEvents[key].push(e);
    }
  });
  let html = '';
  let dayCount = 1;
  let nextCount = 1;
  const totalCells = Math.ceil((firstDay + daysInMonth) / 7) * 7;
  for (let i = 0; i < totalCells; i++) {
    let cls = 'day';
    let num = '';
    let isCurrentMonth = false;
    if (i < firstDay) {
      num = daysInPrev - firstDay + i + 1;
      cls += ' other-month';
      if (i % 7 === 0) cls += ' sunday';
      if (i % 7 === 6) cls += ' saturday';
    } else if (dayCount <= daysInMonth) {
      num = dayCount;
      isCurrentMonth = true;
      const dow = i % 7;
      if (dow === 0) cls += ' sunday';
      if (dow === 6) cls += ' saturday';
      if (dayCount === today.getDate() && currentMonth === today.getMonth() && currentYear === today.getFullYear()) cls += ' today';
      dayCount++;
    } else {
      num = nextCount++;
      cls += ' other-month';
      if (i % 7 === 0) cls += ' sunday';
      if (i % 7 === 6) cls += ' saturday';
    }
    let evHtml = '';
    if (isCurrentMonth && monthEvents[num]) {
      monthEvents[num].forEach(e => {
        const icon = e.source === 'preschool' ? '🏫' : '🗓';
        evHtml += `<div class="event" title="${e.title}">${icon} ${e.title}</div>`;
      });
    }
    html += `<div class="${cls}"><div class="day-num">${num}</div>${evHtml}</div>`;
  }
  document.getElementById('calendarDays').innerHTML = html;
  const thisMonthEvents = allEvents.filter(e => {
    const d = new Date(e.date + 'T00:00:00+09:00');
    return d.getFullYear() === currentYear && d.getMonth() === currentMonth;
  }).sort((a, b) => a.date.localeCompare(b.date));
  if (thisMonthEvents.length === 0) {
    document.getElementById('eventList').innerHTML = '<div class="empty">今月の予定はありません</div>';
  } else {
    document.getElementById('eventList').innerHTML = thisMonthEvents.map(e => {
      const d = new Date(e.date + 'T00:00:00+09:00');
      const dateStr = (d.getMonth()+1) + '月' + d.getDate() + '日';
      const icon = e.source === 'preschool' ? '🏫' : '🗓';
      return `<div class="event-item">
        <div class="info">
          <div class="date">${dateStr}</div>
          <div class="title">${icon} ${e.title}</div>
        </div>
        <button class="delete-btn" onclick="deleteEvent('${e.id}')">✕</button>
      </div>`;
    }).join('');
  }
}
render();
</script>
</body>
</html>
"""
    return html

@app.route("/delete/<event_id>", methods=["POST"])
def delete_event(event_id):
    delete_event_db(event_id)
    return jsonify({"ok": True})

@app.route("/")
def index():
    return "LINE Bot is running! カレンダーは /calendar で確認できます。"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
