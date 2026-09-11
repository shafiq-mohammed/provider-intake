# Provider Document Intake

## Goal
Build a simple workflow that allows a behavioral health provider to upload a professional credential document, such as a license. 

After upload, use an LLM to extract the following structured information where it can be determined: 
Provider name 
License number 
State 
Expiration date 

The user should be able to see the extracted information and which required fields, if any, could not be reliably determined. 
You may decide how uncertainty should be represented and handled. 

## Scope
- Upload jpg, png, pdf. 10 MBs max, otherwise return an error that filesize is too big. Give the user that info upfront about max filesize
- OCR - We want to retrieve raw text. PDF would also contain an image so run it through the OCR. 
- Extraction - LLM to extract the text and make sure it maps to the respective fields provided above
- Per field validation. Flags for missing values, invalid values, corrupted values. Making sure state and expiration dates are valid
- Simple UI to upload, fastapi backend. 

## Constraints

- Stack: Python 3.11, FastAPI, pydantic v2, pytest, ruff. In-memory storage behind an interface. No database.
- The LLM call must sit behind a protocol with a fake implementation so every test runs without network access or an API key.
- The LLM's output is untrusted input: validate and normalize it deterministically after the call. Validation may only downgrade a field's certainty, never upgrade it.
- Errors return a JSON error envelope with a 4xx status. Never 500 for bad input.
- Document contents are never logged.

## Ticket guidance

- Vertical slices, smallest first. T-001 is only the app skeleton, settings, error envelope, and health check.
- Put the real LLM adapter in its own ticket, last, because it needs an API key and the sample documents.
- Tests must cover: all fields found; a field absent from the document; a field the model could not read; the model returning an unparseable date; the model returning a full state name instead of a code; an expired license; the extractor raising.

## Out of scope

Authentication, persistence, batch uploads, human review queue, deployment.
