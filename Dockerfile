FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Use environment variables for secrets
ENV BOT_TOKEN=""
ENV CHAT_ID=""

CMD ["python", "bot.py"]
