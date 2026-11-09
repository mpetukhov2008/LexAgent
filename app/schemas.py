from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FieldName = Literal["platform", "object", "rights", "placement", "demands", "event_date"]
LABELS = {
    "platform": "Площадка",
    "object": "Объект",
    "rights": "Права",
    "placement": "Размещение",
    "demands": "Требования",
    "event_date": "Дата события",
}
QUESTIONS = {
    "platform": "На какой площадке размещена спорная карточка?",
    "object": "Что скопировано: фотографии, инфографика, текст или оформление?",
    "rights": "Кому принадлежат права и какие документы это подтверждают?",
    "placement": "Кто и как добавил контент: продавец, общая карточка или площадка?",
    "demands": "Чего требует заявитель: удалить контент, выплатить компенсацию или иное?",
    "event_date": "Когда обнаружено и зафиксировано предполагаемое нарушение?",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Document(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=12000)


class Intake(StrictModel):
    title: str = Field(min_length=3, max_length=160)
    narrative: str = Field(min_length=20, max_length=12000)
    role: Literal["claimant", "respondent", "unknown"] = "unknown"
    mode: Literal["demo", "yandex", "gigachat"] = "demo"
    search_enabled: bool = False
    search_query: str = Field(default="", max_length=300)
    consent_to_cloud: bool = False
    documents: list[Document] = Field(default_factory=list, max_length=6)
    answers: dict[FieldName, str] = Field(default_factory=dict)
    consent_to_yandex: bool = False
    revision_note: str = Field(default="", max_length=500)

    @field_validator("answers")
    @classmethod
    def answer_lengths(cls, values):
        if any(not v.strip() or len(v) > 1000 for v in values.values()):
            raise ValueError("Ответ должен содержать от 1 до 1000 символов")
        return {k: v.strip() for k, v in values.items()}

    @model_validator(mode="after")
    def total_size(self):
        if len(self.narrative) + sum(len(d.text) for d in self.documents) > 50000:
            raise ValueError("Общий объем текстов превышает 50 000 символов")
        return self


class Clarifications(StrictModel):
    consent_to_cloud: bool = False
    answers: dict[FieldName, str] = Field(min_length=1)
    consent_to_yandex: bool = False

    @field_validator("answers")
    @classmethod
    def lengths(cls, values):
        return Intake.answer_lengths(values)


class ReadDocument(StrictModel):
    source_id: str = Field(min_length=1, max_length=40)


class Fact(StrictModel):
    field: FieldName
    value: str = Field(min_length=1, max_length=1000)
    source_id: str = Field(min_length=1, max_length=40)
    quote: str = Field(min_length=3, max_length=1200)


class RecordFacts(StrictModel):
    facts: list[Fact] = Field(min_length=1, max_length=20)


class AskQuestion(StrictModel):
    field: FieldName
    question: str = Field(min_length=5, max_length=500)


class Finish(StrictModel):
    reason: str = Field(min_length=5, max_length=500)


class Empty(StrictModel):
    pass
