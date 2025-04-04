#!/usr/bin/env python3
from flask import Flask, Response
import threading
import time
import requests

app = Flask(__name__)

# --- Глобальные переменные ---
global_frame = None              # Последний полученный кадр (JPEG)
streaming_enabled = False        # Флаг трансляции (изначально выключена, так как зрителей нет)
frame_cond = threading.Condition()

viewer_count = 0                 # Количество активных зрителей
viewer_lock = threading.Lock()   # Блокировка для изменения viewer_count

# Адрес ESP32‑CAM, который отдает MJPEG‑поток (замените на адрес вашего устройства)
ESP32_STREAM_URL = "http://192.168.0.105/"
# Граница (boundary) должна совпадать с той, что установлена в ESP32‑CAM
BOUNDARY = "123456789000000000000987654321"

# --- Фоновый поток для получения кадров с ESP32-CAM ---
def capture_frames():
    global global_frame, streaming_enabled
    while True:
        if not streaming_enabled:
            # Если трансляция выключена (нет зрителей), ждем 1 секунду и пробуем снова
            time.sleep(1)
            continue
        try:
            print("Подключаюсь к ESP32‑CAM...")
            with requests.get(ESP32_STREAM_URL, stream=True, timeout=10) as r:
                r.raise_for_status()
                stream = r.raw
                stream.decode_content = True
                # Читаем поток, пока трансляция включена
                while streaming_enabled:
                    line = stream.readline()
                    if not line:
                        break
                    # Поиск строки с границей (начало нового кадра)
                    if BOUNDARY.encode() in line:
                        headers = {}
                        # Чтение заголовков кадра
                        while True:
                            header_line = stream.readline()
                            if header_line in (b'\r\n', b'\n', b''):
                                break
                            parts = header_line.decode("utf-8").strip().split(":", 1)
                            if len(parts) == 2:
                                headers[parts[0].strip()] = parts[1].strip()
                        # Чтение содержимого кадра по значению Content-Length
                        if "Content-Length" in headers:
                            content_length = int(headers["Content-Length"])
                            jpeg_frame = stream.read(content_length)
                        else:
                            jpeg_frame = b""
                        # Пропускаем завершающую строку разделителя
                        stream.readline()
                        # Сохраняем кадр и уведомляем ожидающих клиентов
                        with frame_cond:
                            global_frame = jpeg_frame
                            frame_cond.notify_all()
        except Exception as e:
            print("Ошибка в capture_frames:", e)
            time.sleep(5)

# --- Генератор MJPEG-ответа для клиента ---
def generate_frames():
    while True:
        if streaming_enabled:
            with frame_cond:
                # Ждем появления нового кадра
                frame_cond.wait()
                frame = global_frame
            if frame is None:
                continue
            multipart = (
                b"--" + BOUNDARY.encode() + b"\r\n" +
                b"Content-Type: image/jpeg\r\n" +
                b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n" +
                frame + b"\r\n"
            )
            yield multipart
        else:
            # Если трансляция выключена, отдаем последний полученный кадр (статичное изображение)
            if global_frame is not None:
                multipart = (
                    b"--" + BOUNDARY.encode() + b"\r\n" +
                    b"Content-Type: image/jpeg\r\n" +
                    b"Content-Length: " + str(len(global_frame)).encode() + b"\r\n\r\n" +
                    global_frame + b"\r\n"
                )
                yield multipart
                time.sleep(0.5)
            else:
                time.sleep(0.5)

# --- Обертка-генератор для отслеживания количества зрителей ---
def viewer_stream():
    global viewer_count, streaming_enabled
    # При подключении клиента увеличиваем счётчик зрителей
    with viewer_lock:
        viewer_count += 1
        if viewer_count > 0 and not streaming_enabled:
            streaming_enabled = True
            print("Подключился зритель. Трансляция включена.")
    try:
        yield from generate_frames()
    finally:
        # При отключении клиента уменьшаем счётчик
        with viewer_lock:
            viewer_count -= 1
            if viewer_count == 0:
                streaming_enabled = False
                print("Зрители отключились. Трансляция выключена.")

# --- Главная страница – полноэкранный режим, только видео ---
@app.route("/")
def index():
    html = """
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8">
      <title>Видеопоток</title>
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <style>
        html, body {
          margin: 0;
          padding: 0;
          overflow: hidden;
          background: black;
          height: 100%;
          width: 100%;
        }
        #stream {
          width: 100vw;
          height: 100vh;
          object-fit: contain;
          display: block;
        }
      </style>
    </head>
    <body>
      <img id="stream" src="/video_feed" alt="Видеопоток">
      <script>
        // Используем Page Visibility API для остановки трансляции при уходе с вкладки
        document.addEventListener("visibilitychange", function(){
          var streamImg = document.getElementById("stream");
          if(document.hidden) {
            // При уходе с вкладки сбрасываем src, чтобы закрыть соединение
            streamImg.src = "";
          } else {
            // При возврате на вкладку восстанавливаем src и, соответственно, соединение
            streamImg.src = "/video_feed";
          }
        });
      </script>
    </body>
    </html>
    """
    return html

# --- Маршрут, отдающий MJPEG-поток клиентам ---
@app.route("/video_feed")
def video_feed():
    return Response(viewer_stream(),
                    mimetype=f"multipart/x-mixed-replace; boundary={BOUNDARY}")

if __name__ == "__main__":
    # Запускаем фоновый поток для получения кадров с ESP32‑CAM
    t = threading.Thread(target=capture_frames, daemon=True)
    t.start()
    # Запускаем Flask-сервер (режим threaded=True позволяет обслуживать нескольких клиентов)
    app.run(host="0.0.0.0", port=5000, threaded=True)