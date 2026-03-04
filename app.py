import os
import json
import re
import base64
import requests
from datetime import datetime, timedelta
from flask import Flask, request, abort, jsonify, render_template_string
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, TextSendMessage

app = Flask(__name__)

line_bot_api = LineBotApi(os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", ""))
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET", ""))

# シンプルなJSON保存（Renderの一時ストレージ）
EVENTS_FILE = "events.json"

def load_events():
    if os.path.exists(EVENTS_FILE):
        with open(EVENTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def save_events(events):
    with open(EVENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(events, f, ensure_ascii=False, indent=2)

def ocr_image(image_content):
    """Google Cloud Vision APIで画像からテキスト抽出"""
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
    """テキストから年長・全学年の予定を抽出"""
    events = []
    lines = text.split("\n")
    current_date = None
    current_year = datetime.now().year

    # 年長に関係するキーワード
    nencho_keywords = ["年長", "5歳", "ひまわり", "ゆり", "さくら", "全園", "全学年", "全クラス", "全員"]

    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue

        # 日付検出
        date_match = re.search(r'(\d{1,2})[月/／](\d{1,2})', line)
        if date_match:
            month, day = int(date_match.group(1)), int(date_match.group(2))
            try:
                d = datetime(current_year, month, day)
                if d < datetime.now() - timedelta(days=1):
                    d = datetime(current_year + 1, month, day)
                current_date = d
            except:
                pass

        if current_date is None:
            continue

        # 年長・全学年キーワードが含まれる行、または学年指定なし（年少・年中の明示がない）の行
        has_other_grade = any(kw in line for kw in ["年少", "年中", "3歳", "4歳"])
        has_nencho = any(kw in line for kw in nencho_keywords)
        no_grade_specified = not re.search(r'年少|年中|年長|3歳|4歳|5歳', line)

        if has_nencho or (no_grade_specified and current_date):
            # イベント名を抽出（日付・学年キーワード除去）
            title = re.sub(r'\d{1,2}[月/／]\d{1,2}日?', '', line)
            title = re.sub(r'年長|全園|全学年|全クラス|全員|5歳|（.*?）|\(.*?\)', '', title)
            title = re.sub(r'[●◎○・\-\s　]+', ' ', title).strip()
            if len(title) < 2:
                continue
            events.append({
                "id": str(int(datetime.now().timestamp() * 1000)) + str(len(events)),
                "date": current_date.strftime("%Y-%m-%d"),
                "title": title,
                "created_at": datetime.now().isoformat(),
                "source": "preschool"
            })

    # 重複除去
    seen = set()
    unique = []
    for e in events:
        key = e["date"] + e["title"]
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique

def parse_event(text):
    """テキストから日付とイベント名を抽出"""
    # 月/日 or 月月日日 パターン
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
    # 過去の日付なら来年にする
    try:
        event_date = datetime(year, month, day)
        if event_date < datetime.now() - timedelta(days=1):
            event_date = datetime(year + 1, month, day)
    except:
        return None
    
    # イベント名：日付部分を除いたテキスト
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
    # 画像取得
    message_content = line_bot_api.get_message_content(event.message.id)
    image_data = b""
    for chunk in message_content.iter_content():
        image_data += chunk

    # OCR
    text = ocr_image(image_data)
    if not text:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="📷 画像を読み取れませんでした。もう少し明るい場所で撮り直してみてください。"))
        return

    # 年長の予定を抽出
    extracted = extract_nencho_events(text)
    if not extracted:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text="📋 年長・全学年の予定が見つかりませんでした。プリント全体が写るように撮ってみてください。"))
        return

    # 保存
    events = load_events()
    events.extend(extracted)
    save_events(events)

    # 返信
    summary = "\n".join([
        f"📅 {datetime.strptime(e['date'], '%Y-%m-%d').strftime('%m月%d日')}「{e['title']}」"
        for e in extracted[:10]
    ])
    reply = f"✅ プリントから{len(extracted)}件の予定を登録しました！\n\n{summary}\n\nカレンダー👇\n" + os.environ.get("APP_URL", "") + "/calendar"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text
    
    # ヘルプ
    if text in ["ヘルプ", "help", "使い方"]:
        reply = "📅 使い方\n\n「3/4 飲み会」のように日付＋予定を送ると自動登録します！\n\nカレンダー確認はこちら👇\n" + os.environ.get("APP_URL", "") + "/calendar"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return
    
    # イベント解析
    parsed = parse_event(text)
    if parsed:
        events = load_events()
        events.append(parsed)
        save_events(events)
        
        date_str = datetime.strptime(parsed["date"], "%Y-%m-%d").strftime("%m月%d日")
        reply = f"✅ 登録しました！\n📅 {date_str}「{parsed['title']}」\n\nカレンダー👇\n" + os.environ.get("APP_URL", "") + "/calendar"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
    else:
        # 日付が見つからない場合はスルー（反応しない）
        pass

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
  .day { min-height: 64px; padding: 4px; border-top: 1px solid #f0f0f0; border-right: 1px solid #f0f0f0; position: relative; }
  .day:nth-child(7n) { border-right: none; }
  .day-num { font-size: 13px; font-weight: 500; margin-bottom: 2px; }
  .day.sunday .day-num { color: #e53935; }
  .day.saturday .day-num { color: #1565c0; }
  .day.today .day-num { background: #4CAF50; color: white; border-radius: 50%; width: 24px; height: 24px; display: flex; align-items: center; justify-content: center; }
  .day.other-month { background: #fafafa; }
  .day.other-month .day-num { color: #ccc; }
  .event { background: #E8F5E9; color: #2E7D32; font-size: 10px; border-radius: 4px; padding: 2px 4px; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; cursor: pointer; }
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
  const today = new Date();
  const label = currentYear + '年' + (currentMonth + 1) + '月';
  document.getElementById('monthLabel').textContent = label;

  const firstDay = new Date(currentYear, currentMonth, 1).getDay();
  const daysInMonth = new Date(currentYear, currentMonth + 1, 0).getDate();
  const daysInPrev = new Date(currentYear, currentMonth, 0).getDate();

  const monthEvents = {};
  allEvents.forEach(e => {
    const d = new Date(e.date);
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

  // イベントリスト
  const thisMonthEvents = allEvents.filter(e => {
    const d = new Date(e.date);
    return d.getFullYear() === currentYear && d.getMonth() === currentMonth;
  }).sort((a, b) => a.date.localeCompare(b.date));

  if (thisMonthEvents.length === 0) {
    document.getElementById('eventList').innerHTML = '<div class="empty">今月の予定はありません</div>';
  } else {
    document.getElementById('eventList').innerHTML = thisMonthEvents.map(e => {
      const d = new Date(e.date);
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
    events = load_events()
    events = [e for e in events if e["id"] != event_id]
    save_events(events)
    return jsonify({"ok": True})

@app.route("/")
def index():
    return "LINE Bot is running! カレンダーは /calendar で確認できます。"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
