import os
import queue
import sys
import threading
import time
from collections import OrderedDict
import pyperclip
import pytesseract
from PIL import ImageGrab
from PySide6.QtWidgets import QApplication, QWidget, QTextEdit, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QComboBox, QFileDialog, QLineEdit, QMessageBox, QCheckBox
from PySide6.QtCore import Qt, QTimer
try:
    from openai import OpenAI
    OPENAI_SDK = True
except Exception:
    import openai
    OPENAI_SDK = False

class SimpleLRU:
    def __init__(self, capacity=500):
        self.capacity = capacity
        self.cache = OrderedDict()
    def get(self, key):
        if key in self.cache:
            self.cache.move_to_end(key)
            return self.cache[key]
        return None
    def put(self, key, value):
        self.cache[key] = value
        self.cache.move_to_end(key)
        if len(self.cache) > self.capacity:
            self.cache.popitem(last=False)

class FileAdapter:
    def __init__(self, path):
        self.path = path
        self._stop = False
    def stop(self):
        self._stop = True
    def run(self, out_queue):
        try:
            with open(self.path, 'r', encoding='utf-8', errors='ignore') as f:
                f.seek(0, os.SEEK_END)
                while not self._stop:
                    line = f.readline()
                    if line:
                        out_queue.put(line.strip())
                    else:
                        time.sleep(0.1)
        except Exception as e:
            out_queue.put(f"__ERROR__ FileAdapter: {e}")

class OCRAdapter:
    def __init__(self, bbox=(0,0,800,200), interval=0.6):
        self.bbox = bbox
        self.interval = interval
        self._stop = False
        self._last_text = ""
    def stop(self):
        self._stop = True
    def run(self, out_queue):
        while not self._stop:
            try:
                img = ImageGrab.grab(bbox=self.bbox)
                text = pytesseract.image_to_string(img).replace('\r','\n')
                lines = [l.strip() for l in text.split('\n') if l.strip()]
                for l in lines:
                    if l != self._last_text:
                        out_queue.put(l)
                        self._last_text = l
                time.sleep(self.interval)
            except Exception as e:
                out_queue.put(f"__ERROR__ OCRAdapter: {e}")
                time.sleep(1.0)

class ClipboardAdapter:
    def __init__(self, interval=0.15):
        self.interval = interval
        self._stop = False
        self._last = None
    def stop(self):
        self._stop = True
    def run(self, out_queue):
        while not self._stop:
            try:
                txt = pyperclip.paste()
                if isinstance(txt,str) and txt.strip() and txt != self._last:
                    self._last = txt
                    out_queue.put(txt.strip())
                time.sleep(self.interval)
            except Exception as e:
                out_queue.put(f"__ERROR__ ClipboardAdapter: {e}")
                time.sleep(1.0)

class Translator:
    def __init__(self, api_key=None, model="gpt-4.1-mini", system_prompt=None):
        self.api_key = api_key or os.environ.get('OPENAI_API_KEY')
        self.model = model
        self.system_prompt = system_prompt or "You are a helpful translation assistant. Translate game chat while preserving tone, slang, and meaning."
        self.cache = SimpleLRU(capacity=1500)
        self._client = None
        if OPENAI_SDK:
            self._client = OpenAI(api_key=self.api_key)
        else:
            openai.api_key = self.api_key
    def translate(self, text, target_language):
        key = f"{target_language}::{text}"
        cached = self.cache.get(key)
        if cached:
            return cached
        try:
            prompt = f"Translate the following chat message to {target_language}. Preserve tone and slang. Only return the translation text.\n\nMessage: {text}"
            if OPENAI_SDK:
                resp = self._client.chat.completions.create(model=self.model,messages=[{"role":"system","content":self.system_prompt},{"role":"user","content":prompt}],temperature=0.2,max_tokens=800)
                translated = resp.choices[0].message.content.strip()
            else:
                resp = openai.ChatCompletion.create(model=self.model,messages=[{"role":"system","content":self.system_prompt},{"role":"user","content":prompt}],temperature=0.2,max_tokens=800)
                translated = resp['choices'][0]['message']['content'].strip()
            self.cache.put(key, translated)
            return translated
        except Exception as e:
            return f"__ERROR__ Translator: {e}"

class TranslatorApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MultiGame Chat Translator")
        self.setGeometry(200,200,700,420)
        self.setWindowFlags(self.windowFlags() | Qt.WindowStaysOnTopHint)
        self.in_queue = queue.Queue()
        self.out_queue = queue.Queue()
        self.adapter_thread = None
        self.adapter = None
        self.translator = None
        self.worker_thread = None
        self.running = False
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.adapter_select = QComboBox()
        self.adapter_select.addItems(["file","ocr","clipboard"])
        self.source_input = QLineEdit()
        self.source_input.setPlaceholderText("File path or bbox (x,y,w,h) for OCR")
        self.browse_btn = QPushButton("Browse")
        self.browse_btn.clicked.connect(self.on_browse)
        self.lang_select = QComboBox()
        self.lang_select.addItems(["Spanish","French","German","Japanese","Korean","Chinese (Simplified)","Portuguese","Russian","Arabic"])
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.Password)
        self.api_key_input.setPlaceholderText("OpenAI API Key (or set OPENAI_API_KEY env)")
        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self.start)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.clicked.connect(self.stop)
        self.stop_btn.setEnabled(False)
        self.overlay_checkbox = QCheckBox("Overlay mode (compact)")
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Adapter:"))
        top_row.addWidget(self.adapter_select)
        top_row.addWidget(QLabel("Source:"))
        top_row.addWidget(self.source_input)
        top_row.addWidget(self.browse_btn)
        mid_row = QHBoxLayout()
        mid_row.addWidget(QLabel("Target language:"))
        mid_row.addWidget(self.lang_select)
        mid_row.addWidget(QLabel("API Key:"))
        mid_row.addWidget(self.api_key_input)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.start_btn)
        btn_row.addWidget(self.stop_btn)
        btn_row.addWidget(self.overlay_checkbox)
        layout = QVBoxLayout()
        layout.addLayout(top_row)
        layout.addLayout(mid_row)
        layout.addLayout(btn_row)
        layout.addWidget(self.log)
        self.setLayout(layout)
        self.timer = QTimer()
        self.timer.timeout.connect(self._process_queues)
        self.timer.start(120)
    def on_browse(self):
        adapter = self.adapter_select.currentText()
        if adapter == 'file':
            p,_ = QFileDialog.getOpenFileName(self,"Select chat log file",os.path.expanduser('~'))
            if p:
                self.source_input.setText(p)
        elif adapter == 'ocr':
            QMessageBox.information(self,"OCR BBox","Enter bbox as: left,top,right,bottom (example: 100,600,900,900)")
    def start(self):
        if self.running:
            return
        api_key = self.api_key_input.text().strip() or os.environ.get('OPENAI_API_KEY')
        if not api_key:
            QMessageBox.warning(self,"Missing API Key","Please enter your OpenAI API key or set OPENAI_API_KEY env variable.")
            return
        target_language = self.lang_select.currentText()
        adapter_name = self.adapter_select.currentText()
        source = self.source_input.text().strip()
        self.translator = Translator(api_key=api_key,model="gpt-4.1-mini")
        if adapter_name=='file':
            if not source or not os.path.exists(source):
                QMessageBox.warning(self,"Bad file","Please select a valid file path for the chat log.")
                return
            self.adapter = FileAdapter(source)
        elif adapter_name=='ocr':
            if not source:
                QMessageBox.warning(self,"Bad bbox","Enter bbox coordinates in 'Source' field (left,top,right,bottom)")
                return
            try:
                parts = [int(p.strip()) for p in source.split(',')]
                if len(parts)!=4:
                    raise ValueError()
            except Exception:
                QMessageBox.warning(self,"Bad bbox","Invalid bbox format. Use: left,top,right,bottom")
                return
            self.adapter = OCRAdapter(bbox=tuple(parts))
        elif adapter_name=='clipboard':
            self.adapter = ClipboardAdapter()
        else:
            QMessageBox.warning(self,"Adapter","Unknown adapter")
            return
        self.running = True
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.adapter_thread = threading.Thread(target=self.adapter.run,args=(self.in_queue,),daemon=True)
        self.adapter_thread.start()
        self.worker_thread = threading.Thread(target=self._worker,args=(target_language,),daemon=True)
        self.worker_thread.start()
        self.log.append(f"[System] Started adapter: {adapter_name}")
    def stop(self):
        if not self.running:
            return
        self.running = False
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        try:
            if self.adapter:
                self.adapter.stop()
        except Exception:
            pass
        self.log.append("[System] Stopped.")
    def _worker(self,target_language):
        while self.running:
            try:
                item = self.in_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if isinstance(item,str) and item.startswith('__ERROR__'):
                self.out_queue.put(item)
                continue
            translated = self.translator.translate(item,target_language)
            self.out_queue.put((item,translated))
            time.sleep(0.05)
    def _process_queues(self):
        while not self.out_queue.empty():
            obj = self.out_queue.get()
            if isinstance(obj,str) and obj.startswith('__ERROR__'):
                self.log.append(f"[Error] {obj}")
            else:
                orig, trans = obj
                if self.overlay_checkbox.isChecked():
                    self.log.append(f"{trans}")
                else:
                    self.log.append(f"[ORIG] {orig}\n[TRANSLATION] {trans}\n")

def main():
    tpath = os.environ.get('TESSERACT_PATH')
    if tpath:
        pytesseract.pytesseract.tesseract_cmd = tpath
    app = QApplication(sys.argv)
    window = TranslatorApp()
    window.show()
    sys.exit(app.exec_())

if __name__=='__main__':
    main()
