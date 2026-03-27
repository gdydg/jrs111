import re
import os
import time
import threading
import datetime
import pytz
import requests
import schedule
import base64
import urllib.parse
import json
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from flask import Flask, send_file, render_template_string

# --- 配置区 ---
SOURCE_URL = "https://im-imgs-bucket.oss-accelerate.aliyuncs.com/index.js?t_5"
BASE_URL = "http://play.sportsteam368.com"
OUTPUT_M3U_FILE = "/app/output/playlist.m3u"
OUTPUT_TXT_FILE = "/app/output/playlist.txt"
TARGET_KEY = "ABCDEFGHIJKLMNOPQRSTUVWX"
# --------------

app = Flask(__name__)
last_update_time = "尚未更新"

# ==========================================
# 核心：内置轻量级 XXTEA 解密算法 (纯 Python 翻译版)
# ==========================================
def str2long(s):
    v = []
    for i in range(0, len(s), 4):
        val = ord(s[i])
        if i + 1 < len(s): val |= ord(s[i+1]) << 8
        if i + 2 < len(s): val |= ord(s[i+2]) << 16
        if i + 3 < len(s): val |= ord(s[i+3]) << 24
        v.append(val)
    return v

def long2str(v):
    s = ""
    for val in v:
        s += chr(val & 0xff)
        s += chr((val >> 8) & 0xff)
        s += chr((val >> 16) & 0xff)
        s += chr((val >> 24) & 0xff)
    return s

def xxtea_decrypt(data, key):
    if not data: return ""
    v = str2long(data)
    k = str2long(key)
    while len(k) < 4:
        k.append(0)
    
    n = len(v) - 1
    if n < 1: return ""
    z = v[n]
    y = v[0]
    delta = 0x9E3779B9
    q = 6 + 52 // (n + 1)
    sum_val = (q * delta) & 0xffffffff

    while sum_val != 0:
        e = (sum_val >> 2) & 3
        for p in range(n, 0, -1):
            z = v[p - 1]
            mx = (((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ ((sum_val ^ y) + (k[(p & 3) ^ e] ^ z))
            y = v[p] = (v[p] - mx) & 0xffffffff
        p = 0
        z = v[n]
        mx = (((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ ((sum_val ^ y) + (k[(p & 3) ^ e] ^ z))
        y = v[0] = (v[0] - mx) & 0xffffffff
        sum_val = (sum_val - delta) & 0xffffffff

    m = v[-1]
    limit = (len(v) - 1) << 2
    if m < limit - 3 or m > limit:
        return None
    res = long2str(v)
    return res[:m]

def decrypt_id_to_url(encrypted_id):
    """解码并提取真实的播放地址"""
    try:
        decoded_id = urllib.parse.unquote(encrypted_id)
        pad = 4 - (len(decoded_id) % 4)
        if pad != 4: decoded_id += "=" * pad
        
        # 对应 JS: atob() -> base64 解码为 latin1 字符串
        bin_str = base64.b64decode(decoded_id).decode('latin1')
        decrypted_bin = xxtea_decrypt(bin_str, TARGET_KEY)
        
        if decrypted_bin:
            # 对应 JS: decodeURIComponent(escape(str)) -> 解决中文乱码
            json_str = decrypted_bin.encode('latin1').decode('utf-8')
            data = json.loads(json_str)
            return data.get("url")
    except Exception as e:
        print(f"解密失败: {e}")
    return None
# ==========================================


def get_html_from_js(js_url):
    try:
        response = requests.get(js_url, timeout=10)
        response.encoding = 'utf-8'
        return "".join(re.findall(r"document\.write\('(.*?)'\);", response.text))
    except Exception as e:
        print(f"获取 JS 文件失败: {e}")
        return ""

def extract_from_resource_tree(page):
    for frame in page.frames:
        if 'paps.html?id=' in frame.url:
            return frame.url.split('paps.html?id=')[-1]

    resource_urls = page.evaluate("() => performance.getEntriesByType('resource').map(r => r.name)")
    for url in resource_urls:
        if 'paps.html?id=' in url:
            return url.split('paps.html?id=')[-1]
    return None

def generate_playlist():
    global last_update_time
    print(f"[{datetime.datetime.now()}] 开始执行抓取任务...")
    
    html_content = get_html_from_js(SOURCE_URL)
    if not html_content: return

    soup = BeautifulSoup(html_content, 'html.parser')
    matches = soup.find_all('ul', class_='item play d-touch active')
    
    tz = pytz.timezone('Asia/Shanghai')
    now = datetime.datetime.now(tz)
    current_year = now.year
    
    m3u_lines = ["#EXTM3U\n"]
    txt_dict = {} # 用于将 txt 按 JRS-联赛名 分组整理

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
            context = browser.new_context()
            page = context.new_page()
            
            for match in matches:
                try:
                    time_tag = match.find('li', class_='lab_time')
                    if not time_tag: continue
                    
                    match_time_raw = time_tag.text.strip() # 例: 03-20 00:00
                    match_time_str = f"{current_year}-{match_time_raw}"
                    match_dt = tz.localize(datetime.datetime.strptime(match_time_str, "%Y-%m-%d %H:%M"))
                    
                    if abs((match_dt - now).total_seconds()) / 3600 > 3:
                        continue

                    # 1. 抓取联赛名、时间、对阵双方
                    league_tag = match.find('li', class_='lab_events')
                    league_name = league_tag.find('span', class_='name').text.strip() if league_tag else "综合"
                    group_name = f"JRS-{league_name}"
                    
                    home_team = match.find('li', class_='lab_team_home').find('strong').text.strip()
                    away_team = match.find('li', class_='lab_team_away').find('strong').text.strip()
                    
                    channel_name = f"{match_time_raw} {home_team} VS {away_team}"

                    channel_li = match.find('li', class_='lab_channel')
                    target_link = None
                    if channel_li:
                        for a_tag in channel_li.find_all('a', href=True):
                            if 'play.sportsteam368.com' in a_tag['href']:
                                target_link = a_tag['href']
                                break
                    
                    if not target_link: continue

                    detail_resp = requests.get(target_link, timeout=10)
                    detail_soup = BeautifulSoup(detail_resp.text, 'html.parser')
                    
                    play_path = None
                    for a in detail_soup.find_all('a', class_='item ok me'):
                        a_text = a.text.strip()
                        if '高清' in a_text or '蓝光' in a_text:
                            play_path = a.get('data-play')
                            break
                    
                    if not play_path: continue

                    final_url = f"{BASE_URL}{play_path}"
                    
                    page.goto(final_url, wait_until="networkidle", timeout=15000)
                    encrypted_id = extract_from_resource_tree(page)

                    if encrypted_id:
                        # 2. 拿到加密 ID，执行 XXTEA 解密，获得直接播放地址
                        real_stream_url = decrypt_id_to_url(encrypted_id)
                        
                        if real_stream_url:
                            # M3U 追加 (自带分组 group-title 属性)
                            m3u_lines.append(f'#EXTINF:-1 tvg-name="{channel_name}" group-title="{group_name}",{channel_name}\n')
                            m3u_lines.append(f'{real_stream_url}\n')
                            
                            # TXT 追加到对应分组字典
                            if group_name not in txt_dict:
                                txt_dict[group_name] = []
                            txt_dict[group_name].append(f"{channel_name},{real_stream_url}")
                            
                            print(f"成功获取并解密: [{group_name}] {channel_name}")

                except Exception as e:
                    print(f"解析比赛出错: {e}")
                    continue
            
            browser.close()
    except Exception as e:
        print(f"运行出错: {e}")

    # 写入文件
    os.makedirs(os.path.dirname(OUTPUT_M3U_FILE), exist_ok=True)
    with open(OUTPUT_M3U_FILE, 'w', encoding='utf-8') as f:
        f.writelines(m3u_lines)
        
    with open(OUTPUT_TXT_FILE, 'w', encoding='utf-8') as f:
        for group, channels in txt_dict.items():
            f.write(f"{group},#genre#\n")  # TXT 格式的分组头
            for ch in channels:
                f.write(f"{ch}\n")
    
    last_update_time = datetime.datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")
    print("播放列表(M3U/TXT)已更新完成。")


# --- Web 管理后台路由 ---
@app.route('/')
def index():
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>IPTV 抓取管理后台</title>
        <meta charset="utf-8">
        <style>
            body { font-family: Arial, sans-serif; background-color: #f4f7f6; padding: 40px; text-align: center; }
            .container { background: white; padding: 40px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.1); max-width: 500px; margin: auto; }
            .btn { display: inline-block; margin: 10px; padding: 12px 24px; color: white; text-decoration: none; border-radius: 5px; font-weight: bold; }
            .btn-blue { background-color: #007bff; }
            .btn-green { background-color: #28a745; }
        </style>
    </head>
    <body>
        <div class="container">
            <h2>IPTV 自动化抓取系统</h2>
            <p>系统状态: <span style="color: green; font-weight: bold;">运行中 🟢</span></p>
            <p>最后更新: <strong>{{ last_update }}</strong></p>
            <div style="margin-top: 20px;">
                <a href="/m3u" class="btn btn-blue">获取 M3U 订阅</a>
                <a href="/txt" class="btn btn-green">获取 TXT 订阅</a>
            </div>
        </div>
    </body>
    </html>
    """
    return render_template_string(html, last_update=last_update_time)

@app.route('/m3u')
def get_m3u():
    try:
        return send_file(OUTPUT_M3U_FILE, mimetype='application/vnd.apple.mpegurl', as_attachment=False)
    except FileNotFoundError:
        return "M3U 文件尚未生成，请稍后再试。", 404

@app.route('/txt')
def get_txt():
    try:
        return send_file(OUTPUT_TXT_FILE, mimetype='text/plain', as_attachment=False)
    except FileNotFoundError:
        return "TXT 文件尚未生成，请稍后再试。", 404

def run_scheduler():
    schedule.every(1).hours.do(generate_playlist)
    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    threading.Thread(target=generate_playlist, daemon=True).start()
    threading.Thread(target=run_scheduler, daemon=True).start()
    app.run(host="0.0.0.0", port=80)
