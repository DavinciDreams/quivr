from typing import Any, Dict, List, Optional

from langchain.chat_models import ChatOpenAI
from langchain.embeddings.openai import OpenAIEmbeddings
from llm.models.FunctionCall import FunctionCall
from llm.models.OpenAiAnswer import OpenAiAnswer
from logger import get_logger
from models.chat import ChatHistory
from repository.chat.get_chat_history import get_chat_history
from repository.chat.update_chat_history import update_chat_history
from supabase.client import Client, create_client
from vectorstore.supabase import CustomSupabaseVectorStore

from .base import BaseBrainPicking

logger = get_logger(__name__)


def format_answer(model_response: Dict[str, Any]) -> OpenAiAnswer:
    answer = model_response["choices"][0]["message"]
    content = answer["content"]
    function_call = None

    if answer.get("function_call", None) is not None:
        function_call = FunctionCall(
            answer["function_call"]["name"],
            answer["function_call"]["arguments"],
        )

    return OpenAiAnswer(
        content=content,
        function_call=function_call,  # pyright: ignore reportPrivateUsage=none
    )


class OpenAIFunctionsBrainPicking(BaseBrainPicking):
    """
    Class for the OpenAI Brain Picking functionality using OpenAI Functions.
    It allows to initialize a Chat model, generate questions and retrieve answers using ConversationalRetrievalChain.
    """

    # Default class attributes
    model: str = "gpt-3.5-turbo-0613"

    def __init__(
        self,
        model: str,
        chat_id: str,
        temperature: float,
        max_tokens: int,
        brain_id: str,
        user_openai_api_key: str,
        # TODO: add streaming
    ) -> "OpenAIFunctionsBrainPicking":  # pyright: ignore reportPrivateUsage=none
        super().__init__(
            model=model,
            chat_id=chat_id,
            max_tokens=max_tokens,
            user_openai_api_key=user_openai_api_key,
            temperature=temperature,
            brain_id=str(brain_id),
            streaming=False,
        )

    @property
    def openai_client(self) -> ChatOpenAI:
        return ChatOpenAI(
            openai_api_key=self.openai_api_key
        )  # pyright: ignore reportPrivateUsage=none

    @property
    def embeddings(self) -> OpenAIEmbeddings:
        return OpenAIEmbeddings(
            openai_api_key=self.openai_api_key
        )  # pyright: ignore reportPrivateUsage=none

    @property
    def supabase_client(self) -> Client:
        return create_client(
            self.brain_settings.supabase_url, self.brain_settings.supabase_service_key
        )

    @property
    def vector_store(self) -> CustomSupabaseVectorStore:
        return CustomSupabaseVectorStore(
            self.supabase_client,
            self.embeddings,
            table_name="vectors",
            brain_id=self.brain_id,
        )

    def _get_model_response(
        self,
        messages: List[Dict[str, str]],
        functions: Optional[List[Dict[str, Any]]] = None,
    ) -> Any:
        """
        Retrieve a model response given messages and functions
        """
        logger.info("Getting model response")
        kwargs = {
            "messages": messages,
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        if functions:
            logger.info("Adding functions to model response")
            kwargs["functions"] = functions

        return self.openai_client.completion_with_retry(**kwargs)

    def _get_chat_history(self) -> List[Dict[str, str]]:
        """
        Retrieves the chat history in a formatted list
        """
        logger.info("Getting chat history")
        history = get_chat_history(self.chat_id)
        return [
            item
            for chat in history
            for item in [
                {"role": "user", "content": chat.user_message},
                {"role": "assistant", "content": chat.assistant},
            ]
        ]

    def _get_context(self, question: str) -> str:
        """
        Retrieve documents related to the question
        """
        logger.info("Getting context")

        return self.vector_store.similarity_search(
            query=question
        )  # pyright: ignore reportPrivateUsage=none

    def _construct_prompt(
        self, question: str, useContext: bool = False, useHistory: bool = False
    ) -> List[Dict[str, str]]:
        """
        Constructs a prompt given a question, and optionally include context and history
        """
        logger.info("Constructing prompt")
        system_messages = [
            {
                "role": "system",
                "content": """Your name is Max Smart, you are a super intelligent AI designed to act as a second brain and augment the user's intellect with your unparalleled information processing and retrieval skills. Your job is to assist the user, provide helpful answers to user questions, and offer analysis or recommendations on request. If you don't know the answer to a user question, simply say I don't know rather than make up an answer. Use the following context to answer the question:""",
            }
        ]

        if useHistory:
            logger.info("Adding chat history to prompt")
            history = self._get_chat_history()
            system_messages.append(
                {"role": "system", "content": "Previous messages are already in chat."}
            )
            system_messages.extend(history)

        if useContext:
            logger.info("Adding chat context to prompt")
            chat_context = self._get_context(question)
            context_message = f"Here are the documents you have access to: {chat_context if chat_context else 'No document found'}"
            system_messages.append({"role": "user", "content": context_message})

        system_messages.append({"role": "user", "content": question})

        return system_messages

    def generate_answer(self, question: str) -> ChatHistory:
        """
        Main function to get an answer for the given question
        """
        logger.info("Getting answer")
        functions = [
            {
                "name": "get_history_and_context",
                "description": "Get the chat history between you and the user and also get the relevant documents to answer the question. Always use that unless a very simple question is asked that a 5 years old could answer.",
                "parameters": {"type": "object", "properties": {}},
            },
        ]

        # First, try to get an answer using just the question
        response = self._get_model_response(
            messages=self._construct_prompt(question), functions=functions
        )
        formatted_response = format_answer(response)

        # If the model calls for history, try again with history included
        if (
            formatted_response.function_call
            and formatted_response.function_call.name == "get_history"
        ):
            logger.info("Model called for history")
            response = self._get_model_response(
                messages=self._construct_prompt(question, useHistory=True),
                functions=[],
            )

            formatted_response = format_answer(response)

        if (
            formatted_response.function_call
            and formatted_response.function_call.name == "get_history_and_context"
        ):
            logger.info("Model called for history and context")
            response = self._get_model_response(
                messages=self._construct_prompt(
                    question, useContext=True, useHistory=True
                ),
                functions=[],
            )
            formatted_response = format_answer(response)

        # Update chat history
        chat_history = update_chat_history(
            chat_id=self.chat_id,
            user_message=question,
            assistant=formatted_response.content or "",
        )

        return chat_history
