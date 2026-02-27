FROM python:3.11
WORKDIR /app

# Upgrade pipu hned na začátku
RUN pip install --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instalace ffmpeg (potřebuješ ho v kódu pro převod mp3 -> wav)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

COPY . .

# FastAPI start – port musí být 8000, aby to sedělo na tvůj kód
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]