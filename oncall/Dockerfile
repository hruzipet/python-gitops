FROM python:3.11
WORKDIR /app

# Upgrade pipu hned na začátku
RUN pip install --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# FastAPI start (předpokládá, že soubor se jmenuje main.py a FastAPI objekt je 'app')
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "5000"]