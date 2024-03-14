import argparse
import json
import logging
import os

import boto3
import pg8000
import requests
from app.bedrock import calculate_document_embeddings
from app.config import EMBEDDING_CONFIG
from app.repositories.common import _get_table_client
from app.repositories.custom_bot import compose_bot_id, decompose_bot_id
from app.route_schema import type_sync_status
from app.utils import compose_upload_document_s3_path
from embedding.loaders import UrlLoader
from embedding.loaders.base import BaseLoader
from embedding.loaders.s3 import S3FileLoader
from embedding.wrapper import DocumentSplitter, Embedder
from llama_index.node_parser import SentenceSplitter
from ulid import ULID

logging.basicConfig(level=logging.INFO)

BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
MODEL_ID = EMBEDDING_CONFIG["model_id"]
CHUNK_SIZE = EMBEDDING_CONFIG["chunk_size"]
CHUNK_OVERLAP = EMBEDDING_CONFIG["chunk_overlap"]

DB_NAME = os.environ.get("DB_NAME", "postgres")
DB_HOST = os.environ.get("DB_HOST", "")
DB_USER = os.environ.get("DB_USER", "postgres")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "password")
DB_PORT = int(os.environ.get("DB_PORT", 5432))
DOCUMENT_BUCKET = os.environ.get("DOCUMENT_BUCKET", "documents")

METADATA_URI = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")


def get_exec_id() -> str:
    # Get task id from ECS metadata
    # Ref: https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-metadata-endpoint-v4.html#task-metadata-endpoint-v4-enable
    response = requests.get(f"{METADATA_URI}/task")
    data = response.json()
    task_arn = data.get("TaskARN", "")
    task_id = task_arn.split("/")[-1]
    return task_id


def insert_to_postgres(
    bot_id: str,
    contents: list[str],
    sources: list[str],
    embeddings: list[list[float]],
    expired_sources: list[str] = [],
):
    conn = pg8000.connect(
        database=DB_NAME,
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
    )

    try:
        with conn.cursor() as cursor:
            delete_query = "DELETE FROM items WHERE botid = %s AND source IN %s"
            cursor.execute(delete_query, (bot_id, f"({','.join(expired_sources)})",))

            insert_query = f"INSERT INTO items (id, botid, content, source, embedding) VALUES (%s, %s, %s, %s, %s)"
            values_to_insert = []
            for i, (source, content, embedding) in enumerate(
                zip(sources, contents, embeddings)
            ):
                id_ = str(ULID())
                print(f"Preview of content {i}: {content[:200]}")
                values_to_insert.append(
                    (id_, bot_id, content, source, json.dumps(embedding))
                )
            cursor.executemany(insert_query, values_to_insert)
        conn.commit()
        print(f"Successfully inserted {len(values_to_insert)} records.")
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def update_sync_status(
    user_id: str,
    bot_id: str,
    sync_status: type_sync_status,
    sync_status_reason: str,
    last_exec_id: str,
):
    table = _get_table_client(user_id)
    table.update_item(
        Key={"PK": user_id, "SK": compose_bot_id(user_id, bot_id)},
        UpdateExpression="SET SyncStatus = :sync_status, SyncStatusReason = :sync_status_reason, LastExecId = :last_exec_id",
        ExpressionAttributeValues={
            ":sync_status": sync_status,
            ":sync_status_reason": sync_status_reason,
            ":last_exec_id": last_exec_id,
        },
    )


def embed(
    loader: BaseLoader,
    contents: list[str],
    sources: list[str],
    embeddings: list[list[float]],
):
    splitter = DocumentSplitter(
        splitter=SentenceSplitter(
            paragraph_separator=r"\n\n\n",
            chunk_size=CHUNK_SIZE,
            chunk_overlap=CHUNK_OVERLAP,
            # Use length of text as token count for cohere-multilingual-v3
            tokenizer=lambda text: [0] * len(text),
        )
    )
    embedder = Embedder(verbose=True)

    documents = loader.load()
    splitted = splitter.split_documents(documents)
    splitted_embeddings = embedder.embed_documents(splitted)

    contents.extend([t.page_content for t in splitted])
    sources.extend([t.metadata["source"] for t in splitted])
    embeddings.extend(splitted_embeddings)


def main(
    user_id: str,
    bot_id: str,
    sitemap_urls: list[str],
    source_urls: list[str],
    filenames: list[str],
    deprecated_sitemap_urls: list[str],
    deprecated_source_urls: list[str],
    deprecated_filenames: list[str],
):
    exec_id = ""
    try:
        exec_id = get_exec_id()
    except Exception as e:
        print(f"[ERROR] Failed to get exec_id: {e}")
        exec_id = "FAILED_TO_GET_ECS_EXEC_ID"

    update_sync_status(
        user_id,
        bot_id,
        "RUNNING",
        "",
        exec_id,
    )

    status_reason = ""
    try:
        if len(sitemap_urls) + len(source_urls) + len(filenames) == 0:
            status_reason = "No contents to embed."
            update_sync_status(
                user_id,
                bot_id,
                "SUCCEEDED",
                status_reason,
                exec_id,
            )
            return

        # Calculate embeddings using LangChain
        contents = []
        sources = []
        embeddings = []

        if len(source_urls) > 0:
            embed(UrlLoader(source_urls), contents, sources, embeddings)
        if len(sitemap_urls) > 0:
            for sitemap_url in sitemap_urls:
                raise NotImplementedError()
        if len(filenames) > 0:
            for filename in filenames:
                embed(
                    S3FileLoader(
                        bucket=DOCUMENT_BUCKET,
                        key=compose_upload_document_s3_path(user_id, bot_id, filename),
                    ),
                    contents,
                    sources,
                    embeddings,
                )

        print(f"Number of chunks: {len(contents)}")

        # Insert records into postgres

        # Get expired sources
        # TODO: Add sitemap_urls when supported
        expired_sources = []
        if len(source_urls) > 0:
            expired_sources.extend(
                UrlLoader(
                    deprecated_source_urls
                ).get_sources()
            )
        if len(filenames) > 0:
            for filename in deprecated_filenames:
                expired_sources.extend(
                    S3FileLoader(
                        bucket=DOCUMENT_BUCKET,
                        key=compose_upload_document_s3_path(
                            user_id,
                            bot_id,
                            filename),
                    ).get_sources()
                )

        insert_to_postgres(bot_id, contents, sources, embeddings, expired_sources)
        status_reason = "Successfully inserted to vector store."
    except Exception as e:
        print("[ERROR] Failed to embed.")
        print(e)
        update_sync_status(
            user_id,
            bot_id,
            "FAILED",
            f"{e}",
            exec_id,
        )
        return

    update_sync_status(
        user_id,
        bot_id,
        "SUCCEEDED",
        status_reason,
        exec_id,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("new_image", type=str)
    parser.add_argument("old_image", type=str, default=None)
    args = parser.parse_args()

    # `dynamodb.NewImage` from EventBridge Pipes
    new_image = json.loads(args.new_image)

    old_image = json.loads(args.old_image) if args.old_image else None

    knowledge = new_image["Knowledge"]["M"]
    sitemap_urls = [x["S"] for x in knowledge["sitemap_urls"]["L"]]
    source_urls = [x["S"] for x in knowledge["source_urls"]["L"]]
    filenames = [x["S"] for x in knowledge["filenames"]["L"]]

    deprecated_sitemap_urls = []
    deprecated_source_urls = []
    deprecated_filenames = []

    sk = new_image["SK"]["S"]
    bot_id = decompose_bot_id(sk)

    pk = new_image["PK"]["S"]
    user_id = pk

    # If old_image is not None, remove the contents that are already embedded
    if (old_image is not None):
        old_knowledge = old_image["Knowledge"]["M"]
        old_sitemap_urls = [x["S"] for x in old_knowledge["sitemap_urls"]["L"]]
        old_source_urls = [x["S"] for x in old_knowledge["source_urls"]["L"]]
        old_filenames = [x["S"] for x in old_knowledge["filenames"]["L"]]
        print(f"old_source_urls: {old_source_urls}")
        print(f"old_sitemap_urls: {old_sitemap_urls}")
        print(f"old_filenames: {old_filenames}")

        sitemap_urls = list(set(sitemap_urls) - set(old_sitemap_urls))
        source_urls = list(set(source_urls) - set(old_source_urls))
        filenames = list(set(filenames) - set(old_filenames))

        # Find deprecated knowledge by looking for old knowledge that is not in new knowledge
        deprecated_sitemap_urls = list(set(old_sitemap_urls) - set(sitemap_urls))
        deprecated_source_urls = list(set(old_source_urls) - set(source_urls))
        deprecated_filenames = list(set(old_filenames) - set(filenames))

    print(f"source_urls to crawl: {source_urls}")
    print(f"sitemap_urls to crawl: {sitemap_urls}")
    print(f"filenames: {filenames}")

    print(f"source_urls to remove: {deprecated_source_urls}")
    print(f"sitemap_urls to remove: {deprecated_sitemap_urls}")
    print(f"filenames to remove: {deprecated_filenames}")

    main(user_id,
         bot_id,
         sitemap_urls,
         source_urls,
         filenames,
         deprecated_sitemap_urls,
         deprecated_source_urls,
         deprecated_filenames)
