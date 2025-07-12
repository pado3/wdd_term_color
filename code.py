# wdd_term_color.py
# カラー版Weather Data Display端末のCircuitPythonコード
#       w/Seeed XIAO ESP32C6 by @pado3@fedibird.com
# r1.0 2025/07/10 initial release
#
# データソースの0-3行目または4-7行目を240x240LCDの240x200領域へ表示する
# WBGTレベルに応じてバックライトと文字の色を変化させる（環境省準拠）
#
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

# 定数
read_period = 10    # web読み込み間隔, sec
sw_interval = 0.1   # swチェックのインターバル, sec
wdt_timeout = 30    # ウォッチドッグタイマ, sec
blk_duty = 20       # バックライトPWMの明るさ, %
blk_cycle = int(blk_duty * 65535 / 100)     # PWMサイクル(16bit)

# ウォッチドックタイマ起動
microcontroller.watchdog.timeout = wdt_timeout
microcontroller.watchdog.mode = watchdog.WatchDogMode.RESET
microcontroller.watchdog.feed()     # initialize wdt

# LED logic of XIAO is reversed and often confusing, so make clarify
# 外部LEDも同じ論理にする（A:VCC, K:D1 に接続し吸い込ませる, ESP32C6 IOL=28mA）
LED_ON = False
LED_OFF = True

# unicode.futurab.ttf 28dotの読み込み(cpfont.bdfは再配布しない)
# ./otf2bdf unicode.futurab.ttf -o cpfont.bdf -p 28
print("read font : unicode.futurab.ttf 28dot")
font = bitmap_font.load_font("/cpfont.bdf")

# 環境省準拠のWBGT配色, from level 0 to 5, 背景・文字
wbgt_color = [
    [0xFFFFFF, 0x000000],
    [0x228CFF, 0xFFFFFF],
    [0x9FD2FF, 0x000000],
    [0xFAF500, 0x000000],
    [0xFF9602, 0x000000],
    [0xFF2900, 0xFFFFFF]
]

# 無線関係のパラメータはsettings.tomlに隠蔽
ssid = os.getenv("CIRCUITPY_WIFI_SSID")
password = os.getenv("CIRCUITPY_WIFI_PASSWORD")
data_path = os.getenv("DATA_SOURCE")

# Release any resources currently in use for the displays
displayio.release_displays()

# ST7789対応ピン。M154-240240-RGBのGND, VCCに続く順
spi_scl = board.D8      # SCK
spi_sda = board.D10     # MOSI
tft_rst = board.D9      # Reset, MISO is not use
tft_dc = board.D7       # Data / Command
tft_cs = board.D6       # chipselect
tft_blk = board.D3      # backlight, why blk?
spi = busio.SPI(spi_scl, spi_sda)

# LCDのリセット（起動・再起動時に行う）
rst = digitalio.DigitalInOut(tft_rst)
rst.direction = digitalio.Direction.OUTPUT
rst.value = False
time.sleep(0.1)
rst.value = True
time.sleep(0.1)

# Room/Lib.スイッチ定義。プルアップ。Press=LowでLib.
rl_sw = digitalio.DigitalInOut(board.D2)
rl_sw.direction = digitalio.Direction.INPUT
rl_sw.pull = digitalio.Pull.UP

# 警告用の赤LED定義
ledr = digitalio.DigitalInOut(board.D1)
ledr.direction = digitalio.Direction.OUTPUT
ledr.value = LED_ON     # とりまON

# onboard LED（黄色）がライブラリで定義されていないので、microcontrollerから直接叩く
ledy = digitalio.DigitalInOut(mpin.GPIO15)
ledy.direction = digitalio.Direction.OUTPUT
ledy.value = LED_ON     # とりまON

# LCDバックライト定義。実機では外部スイッチで切り離せる（NC時はONになる）
# 当初High/Low二値にしていたが、眩しかったのでPWMに変更した
blk = pwmio.PWMOut(tft_blk, duty_cycle=blk_cycle)
"""
blk = digitalio.DigitalInOut(tft_blk)
blk.direction = digitalio.Direction.OUTPUT
blk.value = True    # とりまON
"""

# LCD定義
lcd_bus = FourWire(spi, command=tft_dc, chip_select=tft_cs)
lcd = ST7789(lcd_bus, width=240, height=240, rowstart=80, rotation=90)
draw = displayio.Group()
lcd.root_group = draw
color_bitmap = displayio.Bitmap(240, 200, 1)
color_palette = displayio.Palette(1)


# エラーが出たときの再起動処理
# 当初数秒で落としていたが、あまり早く落ちると割り込みが効かなくなるのでWDTを使う
def reboot():
    microcontroller.watchdog.feed()     # initialize wdt
    print(f"reset in {wdt_timeout}sec")
    time.sleep(wdt_timeout - 5)     # 描画が間に合うよう早めに出す
    lcd_1line(3, "reboot now")
    time.sleep(wdt_timeout*2)       # この半分で必ずWDTに引っかかる
    # microcontroller.reset()         # WDTに任せることにした残滓


# 文字列を整数に変換するにあたり、データエラーが出がちなので関数化する
def stoi(text):
    try:
        value = int(text)
    except ValueError as e:     # getしたtextが異常だと、ここに落ちる
        lcd_1line(4, "VALUE err")
        print(f"❌ ValueError: {e}\n")
        print(f"{text} is not integer, perhaps server error.")
        reboot()
    return value


# 宅内ネットワークに接続し、取得時分+温湿度+WBGTのデータをもらってくる
def get_data():
    # Initalize wifi, Socket Pool, Request Session
    pool = adafruit_connection_manager.get_radio_socketpool(wifi.radio)
    ssl_context = adafruit_connection_manager.get_radio_ssl_context(wifi.radio)
    requests = adafruit_requests.Session(pool, ssl_context)
    if not wifi.radio.connected:
        print(f"\nConnecting to {ssid}... ", end='')
        try:
            wifi.radio.connect(ssid, password)
        except OSError as e:
            lcd_1line(4, "Wi-Fi error")
            print(f"❌ OSError: {e}\n")
            reboot()
    else:
        print("Wi-Fi is already ", end='')
    print("connected, ", end='')
    rssi = wifi.radio.ap_info.rssi  # available if AP is up
    print(f"RSSI: {rssi} dBm")
    print("request data... ", end='')
    response = requests.get(data_path)
    print("done. ", end='')
    status = response.status_code
    print(f"request status: {status}\n")
    if status != 200:
        lcd_1line(4, "SERVER err")
        print("❌ Server error")
        reboot()
    return response.text.splitlines()


# バックライトのコントロール。1行目のデータをもらって基本的に夜間は消すが危険時は点ける
def blk_ctrl(lvr, dat0):
    hhmm = dat0[5:]
    print(f"it's {hhmm}, room WBGT level {lvr}/5: backlight ", end='')
    hour = stoi(hhmm[0:2])
    if lvr == 5:    # 危険レベルの場合は強制ON
        print("on")
        blk.duty_cycle = blk_cycle
        # blk.value = True    # PWMに変更する前
    elif 6 <= hour and hour < 21:   # 6時台〜20時台はON
        print("on")
        blk.duty_cycle = blk_cycle
        # blk.value = True
    else:
        print("off")
        blk.duty_cycle = 0
        # blk.value = False


# LCDに1行表示する。色はWBGTレベルを使う（基本0, エラー4, reboot3）
def lcd_1line(lv, text):
    print(f"1-line drawing, level:{lv}, text:{text}")
    # background color
    color_palette[0] = wbgt_color[lv][0]
    bg_sprite = displayio.TileGrid(
        color_bitmap, pixel_shader=color_palette, x=0, y=0)
    draw.append(bg_sprite)
    # Draw a label
    text_group = displayio.Group(scale=1, x=5, y=100)
    # text_area = label.Label(
    #     terminalio.FONT, text=text, color=wbgt_color[lv][1])
    text_area = label.Label(font, text=text, color=wbgt_color[lv][1])
    text_group.append(text_area)    # Subgroup for text scaling
    draw.append(text_group)


# LCDに4行表示する。
def lcd_4line(lv, dats):
    print(f"4-line drawing, level:{lv}, text:{dats}")
    # print(f"WBGT level: {lv}/5")
    # background color
    color_palette[0] = wbgt_color[lv][0]
    bg_sprite = displayio.TileGrid(
        color_bitmap, pixel_shader=color_palette, x=0, y=0)
    draw.append(bg_sprite)
    print("bg drew, ", end='')
    # text
    print("line draw: ", end='')
    for j, dat in enumerate(dats):
        text_group = displayio.Group(scale=1, x=5, y=40+40*j)
        # text_area = label.Label(
        #     terminalio.FONT, text=dat, color=wbgt_color[lv][1])
        text_area = label.Label(font, text=dat, color=wbgt_color[lv][1])
        text_group.append(text_area)
        draw.append(text_group)
        print(j+1, end=' ')
        # time.sleep(0)
    print()


# メインルーチン
def main():
    lcd_1line(0, " INITIALIZE")
    print("into infinite loop")
    dats_prev = ""  # 前の表示データ
    while True:
        microcontroller.watchdog.feed()     # reset wdt
        dat = get_data()
        lvr = stoi(dat[3][7])   # ROOM WBGT level 0-5
        blk_ctrl(lvr, dat[0])   # 危険レベルの場合と日中は点灯する
        for i in range(read_period / sw_interval):  # ex. 10s/0.1s=100times
            if 5 == lvr:    # 危険レベルの時は点滅させる（オンボードLEDは見えないけれど）
                ledy.value = not ledy.value
                ledr.value = not ledy.value     # 交互に点ける（お遊びです）
            else:
                ledy.value = LED_OFF
                ledr.value = LED_OFF
            # データが室内・室外の順なので、押すと室外のSW論理を反転させて用いる
            rl = not rl_sw.value
            dats = dat[(4*rl):(4+4*rl)]
            # 描画が遅いので、表示データが変わらないときはスキップ
            if dats != dats_prev:
                dats_prev = dats
                print()
                for d in dats:
                    print(d)
                lvd = stoi(dat[3+4*rl][7])  # 表示データのWBGT level
                lcd_4line(lvd, dats)
            time.sleep(sw_interval)
        print("-=-=-=-=-=-=-=-=-=-=-=-=-=-=-")


# お約束
if __name__ == '__main__':
    print("global process done")
    main()
