FROM python:3.9-slim

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir -r requirements.txt

VOLUME ["/app/db"]
ENV TELEGRAM_TOKEN=$TELEGRAM_TOKEN
ENV TZ=Asia/Shanghai
ENV UPLOAD_ALERT_THRESHOLD_GB=0
ENV DOWNLOAD_ALERT_THRESHOLD_GB=0

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

CMD ["python", "bot.py"]