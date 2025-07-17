"""
wdd_term_color.py
カラー版Weather Data Display端末のCircuitPythonコード
    w/Seeed XIAO ESP32C6 by @pado3@fedibird.com
    r2.0 2025/07/17 red LED to color, LCD brightness from S/W to VR
    r1.1 2025/07/16 refactering, tuning (docstring, brightness, etc.)
    r1.0 2025/07/10 initial release

データソースの0-3行目または4-7行目を240x240LCDの240x200領域へ表示する
WBGTレベルに応じてバックライト・カラーLEDと文字の色を変化させる（環境省準拠）
"""
import board
import busio
import digitalio
import displayio
import os
import microcontroller
import time
import watchdog
import wifi
from fourwire import FourWire
from microcontroller import pin as mpin
# ここまでは標準ライブラリ。これより下はBundleから/libフォルダにコピーする
import adafruit_connection_manager
import adafruit_requests
from adafruit_bitmap_font import bitmap_font
from adafruit_display_text import label
from adafruit_st7789 import ST7789
import neopixel

print("library import done")

"""global定数."""
LOOP_COUNT = 50         # web読み込み待ちのループ回数
SW_INTERVAL = 0.2       # swチェックのインターバル, sec, 約10secループ
WDT_TIMEOUT = 30        # ウォッチドッグタイマ, sec
REBOOT_TIMEOUT = 20     # rebootまでの時間, sec, wdtより余裕をもって短くする
REBOOT_WAIT = 3         # rebootの表示待ち時間
BR_MAX = 50             # npの最大輝度、パーセント

# 環境省準拠のWBGT配色, from level 0 to 5, 背景・文字・LED
# 背景とLEDは白・青・水・黄・橙・赤で、文字は青地と赤地が白で他は黒
# なお、LEDの色順はGRBで、RGB順ではないので注意
WBGT_COLOR = [
    [0xFFFFFF, 0x000000, (16, 16, 16)],
    [0x228CFF, 0xFFFFFF, (0, 0, 64)],
    [0x9FD2FF, 0x000000, (32, 0, 32)],
    [0xFAF500, 0x000000, (48, 48, 0)],
    [0xFF9602, 0x000000, (48, 96, 0)],
    [0xFF2900, 0xFFFFFF, (0, 255, 0)]
]

# unicode.futurab.ttf 28dotの読み込み(cpfont.bdfは再配布しない)
# ./otf2bdf unicode.futurab.ttf -o cpfont.bdf -p 28
print("read font : unicode.futurab.ttf 28dot")
FONT = bitmap_font.load_font("/cpfont.bdf")

# オンボードLEDとバックライトの論理定義（XIAOのオンボは逆論理）
LED_ON = False  # False=ON, True=OFF
LED_OFF = True
BLK_ON = True   # True=ON, False=OFF
BLK_OFF = False

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

# カラーLED(neopixel, PL9823)のオブジェクト定義
ORDER = neopixel.GRB    # pixel color channel order, PL9823はGRB
pixel = neopixel.NeoPixel(board.D1, 1, pixel_order=ORDER)
pixel[0] = WBGT_COLOR[0][2]     # とりま暗い白

# onboard LED（黄色）がライブラリで定義されていないので、microcontrollerから直接叩く
LEDY = digitalio.DigitalInOut(mpin.GPIO15)
LEDY.direction = digitalio.Direction.OUTPUT
LEDY.value = LED_ON     # とりまON

# LCDバックライト定義。実機では外部SWで常時ON。明るさは外部VRで調整する。
BLK = digitalio.DigitalInOut(TFT_BLK)
BLK.direction = digitalio.Direction.OUTPUT
BLK.value = BLK_ON  # とりまON

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
displayio.release_displays()    # should release before SPI definition
spi = busio.SPI(SPI_SCL, SPI_SDA)
lcd_bus = FourWire(spi, command=TFT_DC, chip_select=TFT_CS)
lcd = ST7789(lcd_bus, width=240, height=240, rowstart=80, rotation=90)
color_bitmap = displayio.Bitmap(240, 200, 1)
color_palette = displayio.Palette(1)
# Global変数はここまで


def handle_error(message, exception=None):
    """エラーメッセージを表示し、例外があればその内容を表示してリブートする."""
    microcontroller.watchdog.feed()     # wdtに引っかからないように初期化
    disp_1line(4, message)
    if exception:
        print(f"❌ {type(exception).__name__}: {exception}")
    # 前にはWDTに任せていたが、コードが汚くなるので待ち時間を独立にした
    print(f"reboot in {REBOOT_TIMEOUT}sec")
    time.sleep(REBOOT_TIMEOUT - REBOOT_WAIT)
    disp_1line(3, "reboot now")     # この描画は再起動まで残る
    time.sleep(REBOOT_WAIT)
    microcontroller.reset()


def stoi(text):
    """文字列を整数に変換するにあたり、データエラーが出がちなので関数化した.
    パラメータは文字列で、戻り値は整数.
    文字列が整数に変換できない場合は、エラーメッセージを表示してリブートする.
    """
    try:
        value = int(text)
    except ValueError as e:     # getしたtextが異常だと、ここに落ちる
        print(f"{text} is not integer, perhaps server error.")
        handle_error("VALUE err", e)
    return value


def get_data():
    """宅内ネットワークに接続し、取得時分+温湿度+WBGTのデータをもらってくる.
    取得するデータは4行一組x(室内・室外)の2組.
    室内：1行目に場所と時分、2行目に温度と湿度、3行目に暑さ指数、4行目にWBGTレベル
    室外：5行目に場所と時分、6行目に温度と湿度、7行目に暑さ指数、8行目にWBGTレベル.
    AP接続できないかデータ取得できない場合はメッセージを出してリブートする.
    """
    # Initalize wifi, Socket Pool, Request Session
    pool = adafruit_connection_manager.get_radio_socketpool(wifi.radio)
    ssl_context = adafruit_connection_manager.get_radio_ssl_context(wifi.radio)
    requests = adafruit_requests.Session(pool, ssl_context)
    if not wifi.radio.connected:
        print(f"\nConnecting to {SSID}... ", end='')
        try:
            wifi.radio.connect(SSID, PASSWORD)
        except Exception as e:
            handle_error("Wi-Fi error", e)
        print(f"IP address: {wifi.radio.ipv4_address}")
        rssi = wifi.radio.ap_info.rssi  # available if AP is up
        print(f"RSSI: {rssi} dBm")
    else:
        print("Wi-Fi is already ", end='')
    print("connected, ", end='')
    print("request data... ", end='')
    try:
        response = requests.get(DATA_PATH)
    except Exception as e:
        handle_error("SERVER error", e)
    print("done. ", end='')
    status = response.status_code
    print(f"request status: {status}\n")
    if status != 200:
        handle_error("SERVER err")
    return response.text.splitlines()


def blk_ctrl(lvr, dat0):
    """バックライトのコントロール.
    1行目のデータをもらって基本的に夜間は消すが危険時は点ける.
    lvrは室内のWBGTレベル0-5, dat0は室内の1行目のデータ.
    1行目のデータは時分（hh:mm）を含むので、そこから時間を取り出して
    6時台〜20時台は点灯し、夜間は消灯する。但し、危険レベルでは強制的に点灯する.
    """
    hhmm = dat0[5:]
    print(f"it's {hhmm}, room WBGT level {lvr}/5: backlight ", end='')
    hour = stoi(hhmm[0:2])
    if lvr == 5:    # 危険レベルの場合は強制ON
        print("on")
        BLK.value = BLK_ON
    elif 6 <= hour and hour <= 20:  # 6時台〜20時台はON
        print("on")
        BLK.value = BLK_ON
    else:
        print("off")
        BLK.value = BLK_OFF


def np_ctrl(lvr, dat0, i):
    """バックライトの色と明るさのコントロール.
    1行目のデータをもらって基本的に夜間は消すが危険時は点ける.
    lvrは室内のWBGTレベル0-5, dat0は室内の1行目のデータ, iはループカウンタ.
    ループカウンタ中央値に向けて暗くなり最終値に向けて明るくなる関数は映えるようにした.
    1行目のデータは時分（hh:mm）を含むので、そこから時間を取り出して
    6時台〜20時台は点灯し、夜間は消灯する。但し、危険レベルでは強制的に点灯する.
    """
    hhmm = dat0[5:]
    hour = stoi(hhmm[0:2])
    br = BR_MAX * abs((i - (LOOP_COUNT/2))/(LOOP_COUNT/2))          # 明るさ
    led_d = tuple([int(br*rgb/100) for rgb in WBGT_COLOR[lvr][2]])  # LED点灯色
    np_off = (0, 0, 0)  # neopixel消灯
    if lvr == 5:    # 危険レベルの場合は強制ON
        pass
    elif 6 <= hour and hour <= 20:  # 6時台〜20時台はON
        pass
    else:
        led_d = np_off
    pixel[0] = led_d


def disp_1line(lv, text):
    """LCDに1行表示する.
    色はWBGTレベルを流用する（基本0, エラー4, reboot3）.
    """
    print(f"1-line drawing, level:{lv}, text:{text}")
    draw = displayio.Group()
    lcd.root_group = draw
    # background color
    color_palette[0] = WBGT_COLOR[lv][0]
    bg_sprite = displayio.TileGrid(
        color_bitmap, pixel_shader=color_palette, x=0, y=0)
    draw.append(bg_sprite)
    # Draw a label
    text_group = displayio.Group(scale=1, x=5, y=100)
    text_area = label.Label(FONT, text=text, color=WBGT_COLOR[lv][1])
    text_group.append(text_area)
    draw.append(text_group)


def disp_4line(lv, dats):
    """LCDに4行表示する.
    lvはWBGTレベル0-5, datsは表示するデータのリスト.
    datsは室内または室外の4行のデータで、各行は文字列.
    """
    print(f"4-line drawing, level:{lv}, text:{dats}")
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
        text_area = label.Label(FONT, text=dat, color=WBGT_COLOR[lv][1])
        text_group.append(text_area)
        draw.append(text_group)
        print(j+1, end=' ')
    print()


def wdd_term_loop(dats_prev):
    """動作ループ、実質的なメインルーチン.
    データ取得してLCDの色とLEDの輝度を決め、値が変わっていたら表示更新する.
    """
    microcontroller.watchdog.feed()     # wdt初期化
    dat = get_data()
    lvr = stoi(dat[3][7])   # ROOM WBGT level 0-5
    blk_ctrl(lvr, dat[0])   # 危険レベルの場合と日中は点灯する
    for i in range(LOOP_COUNT):
        # 室内が危険レベルの時はオンボードLEDを点滅させる
        if 5 == lvr:
            LEDY.value = not LEDY.value
        else:
            LEDY.value = LED_OFF
        # カラーLEDを室内WBGTに応じた色で明滅させる。但し夜間は消す
        np_ctrl(lvr, dat[0], i)
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
    print(f"\n-=-=-=-=-=-=-=-=-=-=-=-=-=-=- at {time.time()}")
    return dats_prev    # 最新の表示データを返す


# メインルーチン
def main():
    disp_1line(0, " INITIALIZE")
    print("into infinite loop")
    dats_prev = []  # 前の表示データ
    while True:
        dats_prev = wdd_term_loop(dats_prev)


# お約束のmain()呼び出し
if __name__ == '__main__':
    print("global process done")
    main()
