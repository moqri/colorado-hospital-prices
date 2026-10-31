FROM python:3.12-slim
WORKDIR /app
COPY server.py ingest.py analyze.py hospitals.py sources.json hospital_aliases.json ./
COPY public ./public
ENV HOST=0.0.0.0 PORT=8765 PYTHONUNBUFFERED=1
# data/ holds the downloaded plans and the database; mount a volume at /app/data so it survives restarts
EXPOSE 8765
HEALTHCHECK --interval=60s --timeout=5s CMD python3 -c "import os,urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8765')+'/healthz',timeout=4).status==200 else 1)"
CMD ["python3", "server.py"]
