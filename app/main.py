from fastapi import FastAPI

app = FastAPI(title="agent-runs")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
