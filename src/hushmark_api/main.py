from fastapi import FastAPI
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine
from pydantic import BaseModel

app = FastAPI()
analyzer = AnalyzerEngine()
anonymizer = AnonymizerEngine()

class AnonymizeRequest(BaseModel):
    text: str

@app.post("/anonymize")
def anonymize_text(request: AnonymizeRequest):
    results = analyzer.analyze(
        text=request.text,
        language="en"
    )

    anonymized = anonymizer.anonymize(
        text=request.text,
        analyzer_results=results
    )

    return {
        "text": anonymized.text
    }