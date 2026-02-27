FROM python:3.11
WORKDIR /app

# Nejprve zkopírujeme requirements a nainstalujeme je
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pak zkopírujeme zbytek kódu
COPY . .

# Spuštění FastAPI přes uvicorn (předpokládá, že v main.py máš: app = FastAPI())
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5000"]