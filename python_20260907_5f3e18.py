import tkinter as tk
from tkinter import messagebox
import cv2
import numpy as np
import mss

import time
import threading
import win32gui
import win32process
import psutil
import os
import json
from pynput.keyboard import Controller


pynput_controller = Controller()

# ------------------- 防抖参数配置 -------------------
HOLD_DEBOUNCE = 0.8      # 视觉信号消失后的防抖宽限期（秒）
VISUAL_STREAK = 2        # 视觉信号连续出现多少帧才算确认（防单帧抖动）
# ---------------------------------------------------

# 全局状态
is_running = False
running_thread = None
detector_instance = None

# 轨道配置数据
BASE_Y = 0
LANES_CONFIG = {'s': [0, 0], 'd': [0, 0], 'f': [0, 0], 'j': [0, 0], 'k': [0, 0], 'l': [0, 0]}
Y_CONFIG = [0, 0]
LANE_HALF_WIDTH = 50
CONFIG_FILE = "config.json"

class Detector:
    def __init__(self, ui, process_name, threshold, lanes, y_range, cooldown, min_hold_time, bright_threshold, bright_pixel_count):
        self.ui = ui
        self.process_name = process_name
        self.threshold = threshold
        self.lanes = lanes
        self.y_range = y_range
        self.cooldown = cooldown
        self.min_hold_time = min_hold_time
        self.bright_threshold = bright_threshold      
        self.bright_pixel_count = bright_pixel_count  
        
        self.sct = mss.mss()
        self.running = True
        
        self.holding_keys = {}         
        self.last_press_time = {}      
        self.last_visual_seen = {}     
        self.signal_streak = {}        
        self.last_short_press = {}     
        
        self.templates = self.load_templates()

    def load_templates(self):
        templates = {}
        folder = "templates"
        if not os.path.exists(folder):
            self.ui.update_log("错误：未找到 templates 文件夹，请先创建并放入截图！")
            return {}
        if os.path.exists(os.path.join(folder, "note.png")):
            templates["note"] = cv2.imread(os.path.join(folder, "note.png"), cv2.IMREAD_GRAYSCALE)
        if os.path.exists(os.path.join(folder, "pressed.png")):
            templates["pressed"] = cv2.imread(os.path.join(folder, "pressed.png"), cv2.IMREAD_GRAYSCALE)
        if os.path.exists(os.path.join(folder, "long_end.png")):
            templates["long_end"] = cv2.imread(os.path.join(folder, "long_end.png"), cv2.IMREAD_GRAYSCALE)
        return templates

    def is_target_focused(self):
        try:
            hwnd = win32gui.GetForegroundWindow()
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            return psutil.Process(pid).name().lower() == self.process_name.lower()
        except:
            return False

    def get_roi(self, x_start, x_end):
        monitor = {
            'left': x_start,
            'top': self.y_range[0],
            'width': x_end - x_start,
            'height': self.y_range[1] - self.y_range[0]
        }
        try:
            img = np.array(self.sct.grab(monitor))
            return cv2.cvtColor(img, cv2.COLOR_BGRA2GRAY)
        except:
            return np.zeros((10, 10), dtype=np.uint8)
        
    def run(self):
        while self.running:
            if not self.is_target_focused():
                time.sleep(0.2)
                continue
            current_time = time.time()
            
            for key, (x_start, x_end) in self.lanes.items():
                roi = self.get_roi(x_start, x_end)
                note_template = self.templates.get("note")
                pressed_template = self.templates.get("pressed")
                long_end_template = self.templates.get("long_end")

                # 1. 短按逻辑
                if key not in self.holding_keys and note_template is not None:
                    res = cv2.matchTemplate(roi, note_template, cv2.TM_CCOEFF_NORMED)
                    if np.any(res >= self.threshold):
                        if current_time - self.last_short_press.get(key, 0) > self.cooldown:
                            pynput_controller.press(key)
                            pynput_controller.release(key)
                            self.last_short_press[key] = current_time
                            self.ui.update_log(f"短按: {key.upper()}")

                # 2. 长按信号识别
                is_long_signal = False
                
                # 模板辅助（识别到光圈/结束圈）
                if pressed_template is not None:
                    res = cv2.matchTemplate(roi, pressed_template, cv2.TM_CCOEFF_NORMED)
                    if np.any(res >= self.threshold): 
                        is_long_signal = True
                if not is_long_signal and long_end_template is not None:
                    res = cv2.matchTemplate(roi, long_end_template, cv2.TM_CCOEFF_NORMED)
                    if np.any(res >= self.threshold): 
                        is_long_signal = True

                # 亮度作为长按信号（触发与维持）
                if not is_long_signal:
                    bright_pixels = int(np.sum(roi > self.bright_threshold))
                    # 由于Y区域缩小，像素数量阈值建议设在150-300之间
                    if bright_pixels > self.bright_pixel_count: 
                        is_long_signal = True

                # 视觉防抖（连续帧确认）
                if is_long_signal:
                    self.signal_streak[key] = self.signal_streak.get(key, 0) + 1
                else:
                    self.signal_streak[key] = 0

                is_long_confirmed = self.signal_streak.get(key, 0) >= VISUAL_STREAK

                # 3. 长按执行逻辑
                if is_long_confirmed:
                    self.last_visual_seen[key] = current_time
                    if key not in self.holding_keys:
                        pynput_controller.press(key)
                        self.holding_keys[key] = current_time
                        self.ui.update_log(f"长按开始: {key.upper()} (亮度/模板锁定)")
                    # 长按中不再持续发送 press，防抖

                # 4. 长按释放逻辑
                elif key in self.holding_keys:
                    time_since_seen = current_time - self.last_visual_seen.get(key, 0)
                    time_since_start = current_time - self.holding_keys[key]
                    
                    if time_since_seen > HOLD_DEBOUNCE and time_since_start >= self.min_hold_time:
                        pynput_controller.release(key)
                        del self.holding_keys[key]
                        self.signal_streak.pop(key, None)
                        self.last_visual_seen.pop(key, None)
                        self.ui.update_log(f"长按结束: {key.upper()} (防抖释放)")

            time.sleep(0.01)

    def stop(self):
        for key in self.holding_keys:
            pynput_controller.release(key)
        self.holding_keys.clear()
        self.signal_streak.clear()
        self.last_visual_seen.clear()
        self.running = False

class YanyunApp:
    def __init__(self, root):
        self.root = root
        # 调整窗口高度适配更多滑块
        self.root.title("燕云十六声 - 丝竹雅韵弹琴助手")
        self.root.geometry("1050x750") 
        self.root.resizable(True, True)

        self.process_var = tk.StringVar(value="yysls.exe")
        self.threshold_var = tk.DoubleVar(value=0.85)
        self.cooldown_var = tk.DoubleVar(value=0.05)
        self.min_hold_var = tk.DoubleVar(value=0.5)
        
        self.bright_threshold_var = tk.IntVar(value=120)  # 默认120，避开雪地高亮
        self.bright_pixel_count_var = tk.IntVar(value=200) # 默认200
        
        self.topmost_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="未运行")
        
        self.base_y_var = tk.StringVar(value="未配置")
        self.s_x_var = tk.StringVar(value="未配置")
        self.d_x_var = tk.StringVar(value="未配置")
        self.f_x_var = tk.StringVar(value="未配置")
        self.j_x_var = tk.StringVar(value="未配置")
        self.k_x_var = tk.StringVar(value="未配置")
        self.l_x_var = tk.StringVar(value="未配置")
       
        self.selecting = False
        self.current_select_key = None
        self.log_text = None
        self.build_ui()




    def build_ui(self):
        frame_top = tk.Frame(self.root)
        frame_top.pack(pady=10)
        tk.Label(frame_top, text="目标进程:").pack(side=tk.LEFT)
        tk.Entry(frame_top, textvariable=self.process_var, width=12).pack(side=tk.LEFT, padx=5)
        tk.Checkbutton(frame_top, text="窗口置顶", variable=self.topmost_var, command=self.toggle_topmost).pack(side=tk.LEFT)

        frame_select = tk.LabelFrame(self.root, text="选区配置", padx=10, pady=10)
        frame_select.pack(fill=tk.X, padx=10, pady=5)
        
        tk.Button(frame_select, text="1. 选取基准线 (Y轴)", bg="lightblue", command=self.select_base_y).pack(fill=tk.X, pady=2)
        tk.Label(frame_select, textvariable=self.base_y_var, fg="blue").pack(fill=tk.X)

        tk.Label(frame_select, text="--- 轨道纵线 (只需点中间) ---").pack(fill=tk.X, pady=5)
        
        rows = [('s', self.s_x_var), ('d', self.d_x_var), ('f', self.f_x_var), 
                ('j', self.j_x_var), ('k', self.k_x_var), ('l', self.l_x_var)]
        for key, var in rows:
            row = tk.Frame(frame_select)
            row.pack(fill=tk.X)
            tk.Button(row, text=key.upper(), width=10, command=lambda k=key: self.select_lane(k)).pack(side=tk.LEFT, padx=5)
            tk.Label(row, textvariable=var).pack(side=tk.LEFT)

        frame_config = tk.Frame(self.root)
        frame_config.pack(pady=5)
        tk.Button(frame_config, text="💾 保存配置", width=12, command=self.save_config).pack(side=tk.LEFT, padx=5)
        tk.Button(frame_config, text="📂 读取配置", width=12, command=self.load_config).pack(side=tk.LEFT, padx=5)

        frame_params = tk.LabelFrame(self.root, text="识别参数", padx=10, pady=5)
        frame_params.pack(fill=tk.X, padx=10, pady=5)
        
        # 原有的滑块
        tk.Label(frame_params, text="识别阈值(0.7-0.95):").pack(side=tk.LEFT)
        tk.Scale(frame_params, from_=0.7, to=0.95, resolution=0.01, variable=self.threshold_var, orient=tk.HORIZONTAL, length=130).pack(side=tk.LEFT, padx=5)
        
        tk.Label(frame_params, text="短按冷却(秒):").pack(side=tk.LEFT)
        tk.Scale(frame_params, from_=0.02, to=0.2, resolution=0.01, variable=self.cooldown_var, orient=tk.HORIZONTAL, length=100).pack(side=tk.LEFT, padx=5)
        
        tk.Label(frame_params, text="长按最短时长(秒):").pack(side=tk.LEFT)
        tk.Scale(frame_params, from_=0.1, to=3.0, resolution=0.1, variable=self.min_hold_var, orient=tk.HORIZONTAL, length=100).pack(side=tk.LEFT, padx=5)
        
        tk.Label(frame_params, text="光柱亮度阈值(0-255):").pack(side=tk.LEFT, padx=(5,0))
        tk.Scale(frame_params, from_=60, to=200, resolution=10, variable=self.bright_threshold_var, orient=tk.HORIZONTAL, length=100).pack(side=tk.LEFT, padx=5)
        
        tk.Label(frame_params, text="光柱像素数量:").pack(side=tk.LEFT, padx=(5,0))
        tk.Scale(frame_params, from_=50, to=500, resolution=10, variable=self.bright_pixel_count_var, orient=tk.HORIZONTAL, length=100).pack(side=tk.LEFT, padx=5)

        frame_controls = tk.Frame(self.root)
        frame_controls.pack(pady=10)
        self.btn_start = tk.Button(frame_controls, text="开始运行", bg="lightgreen", width=12, command=self.start_script)
        self.btn_start.pack(side=tk.LEFT, padx=10)
        self.btn_stop = tk.Button(frame_controls, text="停止运行", bg="salmon", width=12, command=self.stop_script, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, padx=10)

        tk.Label(self.root, textvariable=self.status_var, font=("Arial", 12, "bold"), fg="blue").pack(pady=5)
        
        log_frame = tk.Frame(self.root)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        tk.Label(log_frame, text="运行日志 (可滚动):").pack(anchor='w')
        
        scrollbar = tk.Scrollbar(log_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text = tk.Text(log_frame, height=6, state='disabled', yscrollcommand=scrollbar.set) 
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.log_text.yview)

        self.update_log("已添加光柱识别参数调节。")
        self.update_log("雪地太亮导致误触：请将光柱亮度阈值调高（如150），像素数量调高（如300）。")
        self.update_log("图片放置：蓝花图为 note.png，蓝光圈为 pressed.png，金圈为 long_end.png")

    def save_config(self):
        # ... 原有的保存逻辑 ...
        global BASE_Y, LANES_CONFIG, Y_CONFIG
        if BASE_Y == 0:
            messagebox.showwarning("提示", "当前没有已配置的选区，无法保存！")
            return
        data = {
            "process_name": self.process_var.get(),
            "threshold": self.threshold_var.get(),
            "cooldown": self.cooldown_var.get(),
            "min_hold_time": self.min_hold_var.get(),
            "bright_threshold": self.bright_threshold_var.get(), # 🆕 新增保存
            "bright_pixel_count": self.bright_pixel_count_var.get(), # 🆕 新增保存
            "base_y": BASE_Y,
            "lanes": LANES_CONFIG,
            "y_config": Y_CONFIG
        }
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4)
            self.update_log(f"✅ 配置已保存至 {CONFIG_FILE}")
        except Exception as e:
            messagebox.showerror("错误", f"保存配置失败：{e}")

    def load_config(self):
        # ... 原有的读取逻辑 ...
        global BASE_Y, LANES_CONFIG, Y_CONFIG
        if not os.path.exists(CONFIG_FILE):
            messagebox.showerror("错误", "未找到 config.json 配置文件！")
            return
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            BASE_Y = data.get("base_y", 0)
            LANES_CONFIG = data.get("lanes", LANES_CONFIG)
            Y_CONFIG = data.get("y_config", Y_CONFIG)
            self.process_var.set(data.get("process_name", "yysls.exe"))
            self.threshold_var.set(data.get("threshold", 0.85))
            self.cooldown_var.set(data.get("cooldown", 0.05))
            self.min_hold_var.set(data.get("min_hold_time", 0.5))
            self.bright_threshold_var.set(data.get("bright_threshold", 120)) # 🆕 新增读取
            self.bright_pixel_count_var.set(data.get("bright_pixel_count", 200)) # 🆕 新增读取
            
            self.base_y_var.set(f"已配置 (Y: {BASE_Y})")
            display_map = {'s': self.s_x_var, 'd': self.d_x_var, 'f': self.f_x_var, 
                           'j': self.j_x_var, 'k': self.k_x_var, 'l': self.l_x_var}
            for key, var in display_map.items():
                if LANES_CONFIG.get(key, [0, 0])[1] != 0:
                    center_x = (LANES_CONFIG[key][0] + LANES_CONFIG[key][1]) // 2
                    var.set(f"已配置 (X: {center_x})")
            self.update_log("✅ 配置读取成功！")
        except Exception as e:
            messagebox.showerror("错误", f"读取配置失败：{e}")

    def toggle_topmost(self):
        self.root.attributes("-topmost", self.topmost_var.get())

    def update_log(self, message):
        if not self.log_text: return
        def _log():
            self.log_text.config(state='normal')
            self.log_text.insert(tk.END, message + "\n")
            self.log_text.see(tk.END)
            self.log_text.config(state='disabled')
        self.root.after(0, _log)

    def select_base_y(self): self.start_select("BASE_Y")
    def select_lane(self, key): self.start_select(key)

    def start_select(self, key):
        if self.selecting: return
        self.selecting = True
        self.current_select_key = key
        self.root.withdraw()
        self.overlay = tk.Toplevel(self.root)
        self.overlay.attributes('-fullscreen', True)
        self.overlay.attributes('-alpha', 0.2)
        self.overlay.attributes('-topmost', True)
        self.overlay.config(bg='black')
        self.canvas = tk.Canvas(self.overlay, cursor="crosshair", bg='black', highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        text = "请按住左键画一条横向基准线（判定线）" if key == "BASE_Y" else f"请在 {key.upper()} 轨道中间画一条纵向线"
        self.canvas.create_text(self.root.winfo_screenwidth()//2, 30, text=text, fill="red", font=("Arial", 18, "bold"))
        self.start_x = None
        self.start_y = None
        self.rect = None
        self.canvas.bind("<ButtonPress-1>", self.on_mouse_down)
        self.canvas.bind("<B1-Motion>", self.on_mouse_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_mouse_up)
        self.overlay.bind("<Escape>", lambda e: self.cancel_select())

    def on_mouse_down(self, event):
        self.start_x = event.x
        self.start_y = event.y
        if self.rect: self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(self.start_x, self.start_y, self.start_x, self.start_y, outline='red', width=2)

    def on_mouse_drag(self, event):
        self.canvas.coords(self.rect, self.start_x, self.start_y, event.x, event.y)

    def on_mouse_up(self, event):
        global BASE_Y, LANES_CONFIG, Y_CONFIG
        x1, y1, x2, y2 = self.start_x, self.start_y, event.x, event.y
        if self.current_select_key == "BASE_Y":
            BASE_Y = int((y1 + y2) / 2)
            Y_CONFIG = [BASE_Y - 60, BASE_Y + 20]
            self.base_y_var.set(f"已配置 (Y: {BASE_Y})")
            self.update_log(f"基准线配置完成")
        else:
            X_CENTER = int((x1 + x2) / 2)
            left_bound = max(0, X_CENTER - LANE_HALF_WIDTH)
            right_bound = X_CENTER + LANE_HALF_WIDTH
            LANES_CONFIG[self.current_select_key] = [left_bound, right_bound]
            display_map = {'s': self.s_x_var, 'd': self.d_x_var, 'f': self.f_x_var, 
                           'j': self.j_x_var, 'k': self.k_x_var, 'l': self.l_x_var}
            display_map[self.current_select_key].set(f"已配置 (X: {X_CENTER})")
            self.update_log(f"{self.current_select_key.upper()} 轨道配置完成")
        self.cancel_select()

    def cancel_select(self):
        if hasattr(self, 'overlay'): self.overlay.destroy()
        self.selecting = False
        self.current_select_key = None
        self.root.deiconify()
        self.root.attributes("-topmost", self.topmost_var.get())

    def start_script(self):
        global is_running, running_thread, detector_instance
        if BASE_Y == 0:
            messagebox.showerror("错误", "请先选取基准线！")
            return
        if 0 in [LANES_CONFIG[k][1] for k in LANES_CONFIG]:
            messagebox.showerror("错误", "请将6条纵线（S D F J K L）全部配置完成！")
            return
        if is_running: return
        process_name = self.process_var.get().strip()
        threshold = self.threshold_var.get()
        cooldown = self.cooldown_var.get()
        min_hold = self.min_hold_var.get()
        
        bright_threshold = self.bright_threshold_var.get()
        bright_pixel_count = self.bright_pixel_count_var.get()
        
        detector_instance = Detector(self, process_name, threshold, LANES_CONFIG, Y_CONFIG, cooldown, min_hold, bright_threshold, bright_pixel_count)
        if not detector_instance.templates:
            messagebox.showerror("错误", "没有加载到模板图片，请检查templates文件夹！")
            return
        running_thread = threading.Thread(target=detector_instance.run, daemon=True)
        running_thread.start()
        is_running = True
        self.status_var.set(f"运行中 (锁定: {process_name})")
        self.btn_start.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.update_log(f">>> 已开始运行")

    def stop_script(self):
        global is_running, detector_instance
        if not is_running: return
        if detector_instance:
            detector_instance.stop()
            detector_instance = None
        is_running = False
        self.status_var.set("已停止")
        self.btn_start.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)
        self.update_log(">>> 已停止运行。")

    def on_closing(self):
        if is_running:
            if messagebox.askokcancel("退出", "脚本正在运行，确定要退出吗？"):
                self.stop_script()
                self.root.destroy()
        else:
            self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = YanyunApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()