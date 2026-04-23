FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# SQLite database lives in a mounted volume so it persists across container restarts
ENV DATABASE_URL=sqlite:////app/data/chores.db
ENV FLASK_APP=run.py

EXPOSE 5000

CMD ["python", "run.py"]
