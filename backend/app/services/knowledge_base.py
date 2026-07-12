###############################################################################
#  Knowledge Base — LangChain Chroma RAG
###############################################################################

import os
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.document_loaders import DirectoryLoader, TextLoader
from langchain_community.vectorstores import Chroma
from langchain_openai import OpenAIEmbeddings
from app.utils.logger import logger


class KnowledgeBase:
    """基于 Chroma 的本地知识库，文档加载 + 向量检索"""

    def __init__(
        self,
        docs_path: str,
        persist_path: str,
        embedding_model: str,
        api_key: str,
        base_url: str,
    ):
        self.docs_path = docs_path
        self.persist_path = persist_path
        self.embeddings = OpenAIEmbeddings(
            model=embedding_model,
            api_key=api_key or os.getenv("OPENAI_API_KEY", ""),
            base_url=base_url if base_url else None,
        )
        self._vectorstore: Chroma | None = None

    async def initialize(self):
        """加载已有向量存储，不存在则重建"""
        os.makedirs(self.persist_path, exist_ok=True)
        os.makedirs(self.docs_path, exist_ok=True)

        try:
            if os.path.exists(self.persist_path) and os.listdir(self.persist_path):
                self._vectorstore = Chroma(
                    persist_directory=self.persist_path,
                    embedding_function=self.embeddings,
                )
                logger.info(
                    f"Loaded existing Chroma index from {self.persist_path}"
                )
            else:
                await self.rebuild()
        except Exception as e:
            logger.warning(f"Failed to load Chroma index: {e}, rebuilding...")
            await self.rebuild()

    async def rebuild(self):
        """重建整个向量索引"""
        documents = []

        # 从 docs_path 加载文档
        if os.path.isdir(self.docs_path):
            for filename in os.listdir(self.docs_path):
                filepath = os.path.join(self.docs_path, filename)
                if os.path.isfile(filepath):
                    try:
                        with open(filepath, "r", encoding="utf-8") as f:
                            content = f.read()
                    except UnicodeDecodeError:
                        continue
                    documents.append(content)

        if not documents:
            logger.warning("No documents found for knowledge base")
            return

        text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=500, chunk_overlap=50
        )
        chunks = []
        for doc in documents:
            chunks.extend(text_splitter.split_text(doc))

        self._vectorstore = Chroma.from_texts(
            chunks,
            self.embeddings,
            persist_directory=self.persist_path,
        )
        logger.info(f"Knowledge base rebuilt: {len(documents)} docs -> {len(chunks)} chunks")

    async def search(self, query: str, k: int = 3):
        """检索相关文档片段"""
        if self._vectorstore is None:
            return []
        try:
            return self._vectorstore.similarity_search(query, k=k)
        except Exception as e:
            logger.warning(f"Knowledge base search error: {e}")
            return []
