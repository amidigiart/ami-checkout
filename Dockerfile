FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir stripe==15.6.1

COPY checkout_api.py .

EXPOSE 8080

CMD ["python", "checkout_api.py"]
