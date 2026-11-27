FROM python:3.13-slim
WORKDIR /app
COPY server.py dependency_graph.py delivery_process.py context_graph.py ./
COPY static ./static
COPY demo ./demo
USER 65534:65534
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"
CMD ["python", "server.py"]
