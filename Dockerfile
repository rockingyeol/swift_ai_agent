FROM python:3.11-slim

WORKDIR /app

RUN pip install --upgrade pip

COPY pyproject.toml .
RUN pip install -e .

# spaCy 한국어 모델 (Presidio NER)
RUN python -m spacy download ko_core_news_lg

COPY . .

CMD ["python", "-m", "app.main"]
