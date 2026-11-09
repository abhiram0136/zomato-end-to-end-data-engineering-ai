
import os
import json
import requests
import snowflake.connector
from dotenv import load_dotenv

load_dotenv()

# AI MODEL CONFIGURATION
MODEL = "qwen2.5:3b"
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_URL = f"{OLLAMA_HOST.rstrip('/')}/api/chat"

SAMPLE_N = 5

TOPICS = [
    "food quality",
    "delivery",
    "pricing",
    "service",
    "packaging",
    "other"
]

# AI CLASSIFICATION INSTRUCTIONS
SYSTEM_PROMPT = f"""
You classify customer reviews for a food delivery app.

Analyze the customer review and return:

1. sentiment_label: positive, negative, or neutral.
2. sentiment_score: a number between -1.0 and 1.0.
3. topic: exactly one of {TOPICS}.
4. key_issue: a short phrase of 6 words or less describing
   the main issue. If there is no issue, return null.

Rules:
- Negative sentiment must have a negative score.
- Positive sentiment must have a positive score.
- Neutral sentiment should have a score close to zero.
- Choose only one topic from the provided list.
- Do not return additional fields.
- Return valid JSON only.

Required JSON format:
{{
    "sentiment_label": "negative",
    "sentiment_score": -0.8,
    "topic": "delivery",
    "key_issue": "Late delivery"
}}
"""


# SNOWFLAKE CONNECTION
def get_connection():
    return snowflake.connector.connect(
        user=os.getenv("SNOWFLAKE_USER"),
        password=os.getenv("SNOWFLAKE_PASSWORD"),
        account=os.getenv("SNOWFLAKE_ACCOUNT"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE"),
        database=os.getenv("SNOWFLAKE_DATABASE"),
        schema=os.getenv("SNOWFLAKE_SCHEMA"),
    )


# CREATE AI OUTPUT TABLE
def create_output_table(cursor):
    cursor.execute("CREATE SCHEMA IF NOT EXISTS ZOMATO.AI")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ZOMATO.AI.REVIEW_ENRICHED (
            REVIEW_ID STRING,
            SENTIMENT_LABEL STRING,
            SENTIMENT_SCORE FLOAT,
            TOPIC STRING,
            KEY_ISSUE STRING,
            MODEL STRING,
            ENRICHED_AT TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
        )
    """)


# FETCH REVIEWS THAT HAVE NOT BEEN ENRICHED
def get_reviews_to_enrich(cursor):
    cursor.execute(f"""
        SELECT REVIEW_ID, COMMENT
        FROM ZOMATO.RAW.REVIEWS
        WHERE REVIEW_ID NOT IN (
            SELECT REVIEW_ID
            FROM ZOMATO.AI.REVIEW_ENRICHED
        )
        LIMIT {SAMPLE_N}
    """)

    return cursor.fetchall()


# CLASSIFY REVIEW USING LOCAL OLLAMA MODEL
def classify_review(comment):

    response = requests.post(
        OLLAMA_URL,
        json={
            "model": MODEL,
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0
            },
            "messages": [
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT
                },
                {
                    "role": "user",
                    "content": str(comment)
                }
            ]
        },
        timeout=120
    )

    response.raise_for_status()

    answer = response.json()["message"]["content"]

    labels = json.loads(answer)

    # VALIDATE AI RESPONSE
    required_fields = {
        "sentiment_label",
        "sentiment_score",
        "topic",
        "key_issue"
    }

    if not required_fields.issubset(labels):
        raise ValueError("AI response is missing required fields.")

    sentiment = str(labels["sentiment_label"]).lower().strip()
    topic = str(labels["topic"]).lower().strip()
    score = float(labels["sentiment_score"])
    key_issue = labels["key_issue"]

    if sentiment not in ["positive", "negative", "neutral"]:
        raise ValueError(f"Invalid sentiment label: {sentiment}")

    if topic not in TOPICS:
        raise ValueError(f"Invalid topic: {topic}")

    if not -1.0 <= score <= 1.0:
        raise ValueError(f"Invalid sentiment score: {score}")

    if key_issue is not None:
        if not isinstance(key_issue, str):
            raise ValueError("key_issue must be a string or null.")

        if len(key_issue.split()) > 6:
            raise ValueError("key_issue exceeds six words.")

    return {
        "sentiment_label": sentiment,
        "sentiment_score": score,
        "topic": topic,
        "key_issue": key_issue
    }


# SAVE ENRICHED RESULTS INTO SNOWFLAKE
def save_results(cursor, results):

    print(f"Saving {len(results)} enriched reviews to Snowflake...")

    cursor.executemany(
        """
        INSERT INTO ZOMATO.AI.REVIEW_ENRICHED
            (review_id, sentiment_label, sentiment_score,
             topic, key_issue, model)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        results,
    )


# MAIN EXECUTION
def main():

    conn = None
    cursor = None

    try:
        # Connect to Snowflake
        conn = get_connection()
        cursor = conn.cursor()

        print("Connected to Snowflake successfully.")

        # Create output table if it does not exist
        create_output_table(cursor)

        # Fetch unprocessed reviews
        reviews = get_reviews_to_enrich(cursor)

        if len(reviews) == 0:
            print("No new reviews to enrich.")
            return

        print(f"Enriching {len(reviews)} reviews using {MODEL}...")

        results = []
        failed_count = 0

        # Process each review
        for review_id, comment in reviews:

            print(f"\nClassifying review {review_id}: {comment}")

            try:
                labels = classify_review(comment)

                print(f"Labels for review {review_id}: {labels}")

                results.append((
                    review_id,
                    labels["sentiment_label"],
                    labels["sentiment_score"],
                    labels["topic"],
                    labels["key_issue"],
                    MODEL
                ))

            except Exception as e:
                failed_count += 1

                print(
                    f"Error occurred while classifying "
                    f"review {review_id}: {e}"
                )

        # Save successful results
        if results:
            save_results(cursor, results)
            conn.commit()

            print(
                f"\nSuccessfully saved "
                f"{len(results)} enriched reviews to Snowflake."
            )

        else:
            print("\nNo reviews were successfully enriched.")

        # Report failures so Airflow does not mark
        # an unsuccessful enrichment task as successful.
        if failed_count > 0:
            raise RuntimeError(
                f"{failed_count} review(s) failed during AI classification."
            )

        print("\nAI enrichment completed successfully.")

    except Exception:
        if conn is not None:
            conn.rollback()

        raise

    finally:
        if cursor is not None:
            cursor.close()

        if conn is not None:
            conn.close()

        print("Snowflake connection closed.")


# PYTHON ENTRY POINT
if __name__ == "__main__":
    main()
