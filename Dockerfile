FROM python:3.12-slim
WORKDIR /app
COPY *.py ./
COPY static ./static
ENV PORT=8080 DB_PATH=/data/data.sqlite3
VOLUME /data
EXPOSE 8080
CMD ["python", "-u", "server.py"]
