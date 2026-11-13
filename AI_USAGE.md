# AI Usage Disclosure

## Tools used
Claude (Anthropic): Used primarily for repository analysis, implementation, code generation, documentation drafts, and automated testing.

ChatGPT (OpenAI): Used for independent code-review guidance, understanding the implementation, environment setup, troubleshooting, interpreting test results, and checking generated reports.

## What I used them for
Claude was the primary implementation assistant and generated essentially all of the application code under the solution/ directory.

Its contributions included:
1. Inspecting the starter repository, assignment requirements, schemas, and data generator.
2. Designing the database models and Alembic migration.
3. Implementing lead CSV parsing, validation, normalization, and duplicate merging.
4. Implementing event JSONL parsing, deduplication, and ingestion.
5. Implementing orphan-event handling and later resolution.
6. Implementing PII encryption and keyed phone hashing.
7. Developing the reporting logic and command-line interface.
8. Creating the automated test suite, including a reference-model-based test.
9. Preparing initial drafts of the README, SUBMISSION.md, and AI_USAGE.md files.

ChatGPT was used separately to:
1. Review the implementation and discuss potential issues.
2. Explain the architecture and important design decisions.
3. Guide local environment setup and database migration.
4. Troubleshoot test and configuration issues.
5. Interpret ingestion logs and verify the consistency of generated reports.

## What I wrote or decided myself
I selected Assignment A and worked through the requirements and constraints of the task.

My contributions included:
1. Reviewing the generated implementation and its outputs.
2. Setting up the project locally using Python and PostgreSQL.
3. Configuring the required environment variables.
4. Running database migrations.
5. Generating the seed 42 dataset and ingesting both data drops.
6. Running the reporting commands and inspecting all five output files.
7. Checking report totals, rejection counts, and other data-quality metrics for consistency.
8. Re-running report generation and ingestion to verify repeatability and idempotency.
9. Running the local automated test suite.
10. Using explanations and code reviews to understand the implementation and its design decisions.

I did not independently write most of the application code. Claude generated the majority of the implementation, and I used AI assistance throughout development and verification.

## Where the AI was wrong or unhelpful
AI-generated suggestions and outputs were not always correct or directly applicable.

Some examples:
1. Some initial test expectations involving CSV fixtures, workflow sorting, and record counts needed correction after checking them against the assignment requirements and actual behavior.
2. A missing-secrets test interacted with automatic .env loading. This required attention to the difference between environment configuration and test isolation.
3. ChatGPT suggested an additional test using a second dataset seed against the existing database. Because the application protects previously ingested file paths from being re-ingested with different content, that additional run encountered the expected append-only ingestion protection. It was not counted as a successful second-seed verification.

These issues reinforced the importance of checking AI-generated code and suggestions against the actual requirements and observed behavior.

## How I verified AI-generated code
I verified the implementation locally using Python and PostgreSQL.

The local verification included:
1. Successful database migration.
2. Successful generation and ingestion of the seed 42 dataset.
3. Processing of both original data drops.
4. Generation and inspection of all five required reports.
5. Consistency checks across report totals and rejection reasons.
6. Re-running report generation and checking that totals remained unchanged.
7. Re-ingesting the original drops and confirming that already processed inputs were skipped.
8. Successful execution of the full local pytest suite.

Additional reference-model, multiple-seed, benchmark, and mutation checks were reported from Claude's development environment. I did not independently reproduce all of those checks locally.

No official expected-output reference was available, so report verification was based on the assignment requirements, generated input data, and consistency checks across the outputs.

## Acknowledgement
AI tools played a substantial role in both implementation and development support. Their contributions are disclosed above. I reviewed and executed the resulting solution locally and used the verification process to understand its behavior and limitations.