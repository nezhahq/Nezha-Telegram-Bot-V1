FROM python:3.9-slim

WORKDIR /app

COPY . /app

RUN pip install --no-cache-dir -r requirements.txt

VOLUME ["/app"]
ENV TELEGRAM_TOKEN=$TELEGRAM_TOKEN

CMD ["python", "bot.py"]