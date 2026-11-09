
import os
import requests
import numpy as np
import pandas as pd
import streamlit as st
import snowflake.connector
from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

EMBEDDING_MODEL = "nomic-embed-text"
CHAT_MODEL = "qwen2.5:3b"

OLLAMA_HOST = os.getenv(
    "OLLAMA_HOST",
    "http://localhost:11434"
).rstrip("/")

EMBEDDING_URL = f"{OLLAMA_HOST}/api/embeddings"
CHAT_URL = f"{OLLAMA_HOST}/api/chat"

NEW_REVIEWS = 500
TOP_K = 5
CACHE_FILE = "review_embeddings.parquet"


# --------------------------------------------------
# OLLAMA EMBEDDINGS
# --------------------------------------------------

def embed(texts):

    embeddings = []

    for text in texts:

        response = requests.post(
            EMBEDDING_URL,
            json={
                "model": EMBEDDING_MODEL,
                "prompt": str(text)
            },
            timeout=120
        )

        response.raise_for_status()

        embedding = response.json()["embedding"]

        embeddings.append(embedding)

    return embeddings


# --------------------------------------------------
# READ REVIEWS FROM SNOWFLAKE
# --------------------------------------------------

def read_reviews_from_snowflake():

    conn = snowflake.connector.connect(
        account=os.getenv("SNOWFLAKE_ACCOUNT"),
        user=os.getenv("SNOWFLAKE_USER"),
        password=os.getenv("SNOWFLAKE_PASSWORD"),
        warehouse=os.getenv("SNOWFLAKE_WAREHOUSE"),
        database=os.getenv("SNOWFLAKE_DATABASE"),
        schema=os.getenv("SNOWFLAKE_SCHEMA"),
    )

    try:

        query = f"""
                    SELECT REVIEW_ID, CITY, RATING, COMMENT
    FROM ZOMATO.STAGING.STG_REVIEW
    SAMPLE ({NEW_REVIEWS} ROWS)
    WHERE COMMENT IS NOT NULL
"""

        cursor = conn.cursor()

        try:
            df = cursor.execute(query).fetch_pandas_all()
        finally:
            cursor.close()

        df.columns = [col.lower() for col in df.columns]

        return df

    finally:
        conn.close()


# --------------------------------------------------
# LOAD REVIEWS AND GENERATE EMBEDDINGS
# --------------------------------------------------

@st.cache_data
def load_reviews():

    if os.path.exists(CACHE_FILE):

        return pd.read_parquet(CACHE_FILE)

    df = read_reviews_from_snowflake()

    if df.empty:
        return df

    print("Generating review embeddings using Ollama...")

    df["embedding"] = embed(
        df["comment"].fillna("").astype(str).tolist()
    )

    df.to_parquet(CACHE_FILE, index=False)

    print("Review embeddings generated successfully.")

    return df


# --------------------------------------------------
# STREAMLIT INTERFACE
# --------------------------------------------------

st.title("Chat with your Zomato Reviews")

st.caption(
    f"Searching {NEW_REVIEWS} reviews, "
    f"answering with {CHAT_MODEL} using Ollama"
)


# --------------------------------------------------
# COSINE SIMILARITY
# --------------------------------------------------

def cosine_similarity(vec_a, vec_b):

    vec_a = np.array(vec_a)
    vec_b = np.array(vec_b)

    denominator = (
        np.linalg.norm(vec_a) *
        np.linalg.norm(vec_b)
    )

    if denominator == 0:
        return 0.0

    return np.dot(vec_a, vec_b) / denominator


# --------------------------------------------------
# FIND SIMILAR REVIEWS
# --------------------------------------------------

def find_similar_reviews(question, df):

    question_vector = embed([question])[0]

    scores = []

    for review_vector in df["embedding"]:

        scores.append(
            cosine_similarity(
                question_vector,
                review_vector
            )
        )

    df = df.copy()

    df["score"] = scores

    return df.nlargest(TOP_K, "score")


# --------------------------------------------------
# GENERATE ANSWER USING QWEN
# --------------------------------------------------

def ask_llm(question, top_reviews):

    context = ""

    for _, row in top_reviews.iterrows():

        context += (
            f"({row['city']}, {row['rating']} stars) "
            f"{row['comment']}\n"
        )

    system_prompt = """
    You are a customer review analysis assistant for Zomato.

    Answer ONLY using the customer reviews provided.

    Instructions:
    1. Be concise and clear.
    2. Use the reviews as your evidence.
    3. Do not invent information.
    4. If the reviews do not contain the answer,
       clearly say that the available reviews
       do not provide enough information.
    """

    user_prompt = f"""
    Question: {question}

    Customer Reviews:
    {context}

    Answer the question using only these reviews.
    """

    response = requests.post(
        CHAT_URL,
        json={
            "model": CHAT_MODEL,
            "stream": False,
            "options": {
                "temperature": 0.2
            },
            "messages": [
                {
                    "role": "system",
                    "content": system_prompt
                },
                {
                    "role": "user",
                    "content": user_prompt
                }
            ]
        },
        timeout=180
    )

    response.raise_for_status()

    return response.json()["message"]["content"]


# --------------------------------------------------
# MAIN STREAMLIT APPLICATION
# --------------------------------------------------

try:

    review_df = load_reviews()

    if review_df.empty:

        st.warning("No customer reviews found in Snowflake.")

    else:

        st.success(
            f"Loaded {len(review_df)} customer reviews."
        )

        question = st.text_input(
            "Ask a question about your reviews:",
            placeholder=(
                "e.g. What are the most common "
                "complaints about delivery?"
            )
        )

        if question:

            with st.spinner("Searching relevant reviews..."):

                top_reviews = find_similar_reviews(
                    question,
                    review_df
                )

            with st.spinner("Generating answer using Qwen..."):

                answer = ask_llm(
                    question,
                    top_reviews
                )

            st.markdown("**Answer:**")

            st.write(answer)

            with st.expander(
                "Reviews used to build this answer"
            ):

                st.dataframe(
                    top_reviews[
                        ["city", "rating", "comment"]
                    ],
                    hide_index=True
                )

except Exception as e:

    st.error(f"An error occurred: {e}")
