"""
wdd_term_color.py
カラー版Weather Data Display端末のCircuitPythonコード
    w/Seeed XIAO ESP32C6 by @pado3@fedibird.com
    r1.1 2025/07/16 refactering, tuning (docstring, brightness, etc.)
    r1.0 2025/07/10 initial release

データソースの0-3行目または4-7行目を240x240LCDの240x200領域へ表示する
WBGTレベルに応じてバックライトと文字の色を変化させる（環境省準拠）
"""
import board
import busio
import digitalio
import displayio
import os
import pwmio
import microcontroller
# import terminalio   # use for default font
import time
import watchdog
import wifi
from fourwire import FourWire
from microcontroller import pin as mpin
import adafruit_connection_manager              # lib
import adafruit_requests                        # lib
from adafruit_bitmap_font import bitmap_font    # lib
from adafruit_display_text import label         # lib
from adafruit_st7789 import ST7789              # lib

print("library import done")

"""global定数."""
LOOP_COUNT = 50         # web読み込み待ちのループ回数
SW_INTERVAL = 0.2       # swチェックのインターバル, sec, 約10secループ
WDT_TIMEOUT = 30        # ウォッチドッグタイマ, sec
REBOOT_TIMEOUT = 20     # rebootまでの時間, sec, wdtより余裕をもって短くする
BLK_DUTY = 10           # バックライトPWMの明るさ, %, 20=>10
BLK_CYCLE = int(BLK_DUTY * 65535 / 100)     # PWMサイクル(16bit)

# 環境省準拠のWBGT配色, from level 0 to 5, 背景・文字
# 背景は順に白・青・水・黄・橙・赤で、文字は青地と赤地が白・他は黒
WBGT_COLOR = [
    [0xFFFFFF, 0x000000],
    [0x228CFF, 0xFFFFFF],
    [0x9FD2FF, 0x000000],
    [0xFAF500, 0x000000],
    [0xFF9602, 0x000000],
    [0xFF2900, 0xFFFFFF]
]

# unicode.futurab.ttf 28dotの読み込み(cpfont.bdfは再配布しない)
# ./otf2bdf unicode.futurab.ttf -o cpfont.bdf -p 28
print("read font : unicode.futurab.ttf 28dot")
FONT = bitmap_font.load_font("/cpfont.bdf")

# LED logic of XIAO is reversed and often confusing, so make clarify.
# 外部LEDも同じ論理にする（A:VCC, K:D1 に接続し吸い込ませる, ESP32C6 IOL=28mA）
LED_ON = False
LED_OFF = True

# 無線関係のパラメータはsettings.tomlに隠蔽
SSID = os.getenv("CIRCUITPY_WIFI_SSID")
PASSWORD = os.getenv("CIRCUITPY_WIFI_PASSWORD")
DATA_PATH = os.getenv("DATA_SOURCE")

# ST7789対応ピン。M154-240240-RGBのGND, VCCに続く順
SPI_SCL = board.D8      # SCK
SPI_SDA = board.D10     # MOSI
TFT_RST = board.D9      # Reset, MISO is not use
TFT_DC = board.D7       # Data / Command
TFT_CS = board.D6       # chipselect
TFT_BLK = board.D3      # backlight, why blk?

# Room/Lib.スイッチ定義。プルアップ。Press=LowでLib.
RL_SW = digitalio.DigitalInOut(board.D2)
RL_SW.direction = digitalio.Direction.INPUT
RL_SW.pull = digitalio.Pull.UP

# 警告用の赤LED定義.
LEDR = digitalio.DigitalInOut(board.D1)
LEDR.direction = digitalio.Direction.OUTPUT
LEDR.value = LED_ON     # とりまON

# onboard LED（黄色）がライブラリで定義されていないので、microcontrollerから直接叩く
LEDY = digitalio.DigitalInOut(mpin.GPIO15)
LEDY.direction = digitalio.Direction.OUTPUT
LEDY.value = LED_ON     # とりまON

# LCDバックライト定義。実機では外部SWで切り離せる（NC時はONになる）
# 当初High/Low二値にしていたが眩しかったのでPWMに変更した。但し外部SWではHighになる問題あり
BLK = pwmio.PWMOut(TFT_BLK, duty_cycle=BLK_CYCLE)
"""
# もしかしたら可変抵抗で明るさ調整するかも知れないので残しておく
BLK = digitalio.DigitalInOut(TFT_BLK)
BLK.direction = digitalio.Direction.OUTPUT
BLK.value = True    # とりまON
"""

# ウォッチドッグタイマ起動
microcontroller.watchdog.timeout = WDT_TIMEOUT
microcontroller.watchdog.mode = watchdog.WatchDogMode.RESET
microcontroller.watchdog.feed()    # wdt初期化

# LCDのリセットとバス定義（SPI, LCD）
print("release & reset LCD")
RST = digitalio.DigitalInOut(TFT_RST)
RST.direction = digitalio.Direction.OUTPUT
RST.value = False
time.sleep(0.1)
RST.value = True
time.sleep(0.1)
displayio.release_displays()    # shold release before SPI definition
spi = busio.SPI(SPI_SCL, SPI_SDA)
lcd_bus = FourWire(spi, command=TFT_DC, chip_select=TFT_CS)
lcd = ST7789(lcd_bus, width=240, height=240, rowstart=80, rotation=90)
# Copilotに、drawは関数内でglobal宣言する方が初期化が確実だと指摘された
# draw = displayio.Group()
# lcd.root_group = draw
color_bitmap = displayio.Bitmap(240, 200, 1)
color_palette = displayio.Palette(1)
# Global変数はここまで


def handle_error(message, exception=None):
    """エラー処理の共通化.
    エラーメッセージを表示し、例外があればその内容を表示してリブートする."""
    microcontroller.watchdog.feed()     # wdt初期化
    disp_1line(4, message)
    if exception:
        print(f"❌ {type(exception).__name__}: {exception}")
    # 前にはWDTに任せていたが、コードが汚くなるので待ち時間を独立にした
    print(f"reset in {REBOOT_TIMEOUT}sec")
    time.sleep(REBOOT_TIMEOUT)
    disp_1line(3, "reboot now")     # この描画は再起動まで残る
    microcontroller.reset()


def stoi(text):
    """文字列を整数に変換するにあたり、データエラーが出がちなので関数化した.
    パラメータは文字列で、戻り値は整数.
    文字列が整数に変換できない場合は、エラーメッセージを表示してリブートする."""
    try:
        value = int(text)
    except ValueError as e:     # getしたtextが異常だと、ここに落ちる
        print(f"{text} is not integer, perhaps server error.")
        handle_error("VALUE err", e)
    return value


def get_data():
    """宅内ネットワークに接続し、取得時分+温湿度+WBGTのデータをもらってくる.
    取得するデータは4行一組x(室内・室外)の2組で、
    1行目は場所（室内）と時分、2行目は温度と湿度、3行目は暑さ指数、4行目はWBGTレベル、
    5行目は場所（室外）と時分、6行目は温度と湿度、7行目は暑さ指数、8行目はWBGTレベル。
    AP接続できないかデータ取得できない場合はメッセージを出してリブートする."""
    # Initalize wifi, Socket Pool, Request Session
    pool = adafruit_connection_manager.get_radio_socketpool(wifi.radio)
    ssl_context = adafruit_connection_manager.get_radio_ssl_context(wifi.radio)
    requests = adafruit_requests.Session(pool, ssl_context)
    if not wifi.radio.connected:
        print(f"\nConnecting to {SSID}... ", end='')
        try:
            wifi.radio.connect(SSID, PASSWORD)
        except OSError as e:
            handle_error("Wi-Fi error", e)
        print(f"IP address: {wifi.radio.ipv4_address}")
        rssi = wifi.radio.ap_info.rssi  # available if AP is up
        print(f"RSSI: {rssi} dBm")
    else:
        print("Wi-Fi is already ", end='')
    print("connected, ", end='')
    print("request data... ", end='')
    response = requests.get(DATA_PATH)
    print("done. ", end='')
    status = response.status_code
    print(f"request status: {status}\n")
    if status != 200:
        handle_error("SERVER err", f"status: {status}")
    return response.text.splitlines()


def blk_ctrl(lvr, dat0):
    """バックライトのコントロール.
    1行目のデータをもらって基本的に夜間は消すが危険時は点ける.
    lvrは室内のWBGTレベル0-5, dat0は室内の1行目のデータ.
    1行目のデータは時分（hh:mm）を含むので、そこから時間を取り出して
    6時台〜20時台は点灯し、夜間は消灯する。但し、危険レベルでは強制的に点灯する."""
    hhmm = dat0[5:]
    print(f"it's {hhmm}, room WBGT level {lvr}/5: backlight ", end='')
    hour = stoi(hhmm[0:2])
    if lvr == 5:    # 危険レベルの場合は強制ON
        print("on")
        BLK.duty_cycle = BLK_CYCLE
        # BLK.value = True    # PWMに変更する前
    elif 6 <= hour and hour <= 20:  # 6時台〜20時台はON
        print("on")
        BLK.duty_cycle = BLK_CYCLE
        # BLK.value = True
    else:
        print("off")
        BLK.duty_cycle = 0
        # BLK.value = False


def disp_1line(lv, text):
    """LCDに1行表示する.
    色はWBGTレベルを流用する（基本0, エラー4, reboot3）"""
    print(f"1-line drawing, level:{lv}, text:{text}")
    # Copilotの指摘により、関数内で初期化するように変更
    global draw
    draw = displayio.Group()
    lcd.root_group = draw
    # background color
    color_palette[0] = WBGT_COLOR[lv][0]
    bg_sprite = displayio.TileGrid(
        color_bitmap, pixel_shader=color_palette, x=0, y=0)
    draw.append(bg_sprite)
    # Draw a label
    text_group = displayio.Group(scale=1, x=5, y=100)
    # text_area = label.Label(
    #     terminalio.FONT, text=text, color=WBGT_COLOR[lv][1])
    text_area = label.Label(FONT, text=text, color=WBGT_COLOR[lv][1])
    text_group.append(text_area)
    draw.append(text_group)


def disp_4line(lv, dats):
    """LCDに4行表示する.
    lvはWBGTレベル0-5, datsは表示するデータのリスト.
    datsは室内または室外の4行のデータで、各行は文字列."""
    print(f"4-line drawing, level:{lv}, text:{dats}")
    # Copilotの指摘により、関数内で初期化するように変更
    global draw
    draw = displayio.Group()
    lcd.root_group = draw
    # background color
    color_palette[0] = WBGT_COLOR[lv][0]
    bg_sprite = displayio.TileGrid(
        color_bitmap, pixel_shader=color_palette, x=0, y=0)
    draw.append(bg_sprite)
    print("bg drew, ", end='')
    # text
    print("line draw: ", end='')
    for j, dat in enumerate(dats):
        text_group = displayio.Group(scale=1, x=5, y=40+40*j)
        # text_area = label.Label(
        #     terminalio.FONT, text=dat, color=WBGT_COLOR[lv][1])
        text_area = label.Label(FONT, text=dat, color=WBGT_COLOR[lv][1])
        text_group.append(text_area)
        draw.append(text_group)
        print(j+1, end=' ')
    print()


def wdd_term_loop(dats_prev):
    """動作ループ、実質的なメインルーチン.
    データ取得してLCDの色とLEDの輝度を決め、値が変わっていたら表示更新する"""
    microcontroller.watchdog.feed()     # wdt初期化
    dat = get_data()
    lvr = stoi(dat[3][7])   # ROOM WBGT level 0-5
    blk_ctrl(lvr, dat[0])   # 危険レベルの場合と日中は点灯する
    # ループ内に処理時間が入るので、全然時間管理になっていなかった
    # for i in range(read_period / sw_interval):  # ex. 10s/0.1s=100times
    # ちゃんと時間管理しようとするなら、こうすべき
    # time_prev = time.time()
    # while time.time() - time_prev < READ_PERIOD:
    # ただ、時間管理しようとしても中途半端でしっくりこず、回数にすることで明確化
    for i in range(LOOP_COUNT):
        # 室内が危険レベルの時は点滅させる（オンボードLEDは見えないけれど）
        if 5 == lvr:
            LEDY.value = not LEDY.value
            LEDR.value = not LEDY.value     # 交互に点ける（お遊びです）
        else:
            LEDY.value = LED_OFF
            LEDR.value = LED_OFF
        # print(f"delta:{time.time() - time_prev}, peri:{READ_PERIOD}")
        # データが室内・室外の順なので、室外=FalseのSW論理を反転させて用いる
        rl = not RL_SW.value
        dats = dat[(4*rl):(4+4*rl)]     # 室内0-3行目、室外4-7行目
        # 描画が遅いので、表示データが変わらないときはスキップ
        if dats != dats_prev:
            print(f"\n-=-=-=-=-=-=-=-=-=-=-=-=-=-=- at {time.time()}")
            dats_prev = dats
            for d in dats:
                print(d)
            lvd = stoi(dat[3+4*rl][7])  # 表示データのWBGT level
            disp_4line(lvd, dats)
        time.sleep(SW_INTERVAL)
    # print(time.time(), end=' ')
    print(f"\n-=-=-=-=-=-=-=-=-=-=-=-=-=-=- at {time.time()}")
    return dats_prev    # 最新の表示データを返す


# メインルーチン
def main():
    disp_1line(0, " INITIALIZE")
    print("into infinite loop")
    dats_prev = ""  # 前の表示データ
    while True:
        dats_prev = wdd_term_loop(dats_prev)


# お約束のmain()呼び出し
if __name__ == '__main__':
    print("global process done")
    main()
