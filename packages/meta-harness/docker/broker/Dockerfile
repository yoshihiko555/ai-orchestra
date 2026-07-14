FROM gcr.io/distroless/python3-debian12:nonroot@sha256:7d1042ce588ab97019fe95c24ffca7bc5a82ccdac572511d5e09bda4435c89c5

WORKDIR /app
COPY broker.py /app/broker.py

ENTRYPOINT ["/usr/bin/python3", "/app/broker.py"]
