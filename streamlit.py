import streamlit as st
import json
import _snowflake
from snowflake.snowpark.context import get_active_session

session = get_active_session()

# ================================ START SETUP TRUST3 CLIENT PACKAGES ================================

def setup_python_packages():
    """
    Sets up Python packages by downloading them from Snowflake stage and extracting them.
    Uses file locking to ensure thread-safe extraction.
    """
    import fcntl
    import os
    import sys
    import threading
    import zipfile

    # List of packages to install
    list_of_packages = ["paig_common", "paig_client"]

    # Download packages from Snowflake stage
    for pkg in list_of_packages:
        session.file.get(f"@PYTHON_PACKAGES/{pkg}.zip", os.getcwd())

    # File lock class for synchronizing write access to /tmp
    class FileLock:
        def __enter__(self):
            self._lock = threading.Lock()
            self._lock.acquire()
            self._fd = open('/tmp/lockfile.LOCK', 'w+') 
            fcntl.lockf(self._fd, fcntl.LOCK_EX)

        def __exit__(self, type, value, traceback):
            self._fd.close()
            self._lock.release()

    # Get paths
    import_dir = os.getcwd()
    extracted = '/tmp/python_pkg_dir'

    # Extract packages under file lock
    with FileLock():
        for pkg in list_of_packages:
            if not os.path.isdir(extracted + f"/{pkg}"):
                zip_file_path = import_dir + f"/{pkg}.zip"
                with zipfile.ZipFile(zip_file_path, 'r') as myzip:
                    myzip.extractall(extracted)

    # Add extracted packages to Python path
    print(f"Adding {extracted} to Python path")
    sys.path.append(extracted)

setup_python_packages()

# ================================ END SETUP TRUST3 CLIENT PACKAGES ================================

# ================================ START TRUST3 SETUP AND HELPER FUNCTIONS ================================

# Trust3 Imports
from paig_client import client as trust3_client
from paig_client.model import ConversationType
import paig_client.exception
import uuid
import ast

# Get current user and its current role
CURRENT_SNOWFLAKE_USER = st.experimental_user["user_name"].lower()
CURRENT_SNOWFLAKE_USER_ROLE = session.get_current_role().strip('"').lower()

# Trust3 Setup
trust3_client.setup(frameworks=[])

# Trust3 Config
TRUST3_SERVER_BASE_URL = "<your-trust3-server-base-url>"
SNOWFLAKE_PAT_TOKEN = "<your-snowflake-pat-token>"
TRUST3_AI_APP_API_KEY = "<your-trust3-ai-app-api-key>"

# Trust3 App Setup
trust3_ai_app = trust3_client.setup_app(
    endpoint=TRUST3_SERVER_BASE_URL,
    application_config_api_key=TRUST3_AI_APP_API_KEY,
    snowflake_pat_token=SNOWFLAKE_PAT_TOKEN)

def get_conversation_thread_id():
    # Get thread id from Trust3
    return str(uuid.uuid4())

def clean_error_message(message: str) -> str:
    prefix = "AccessControlException: ERROR: PAIG-400004: "
    if "denied" not in message.lower() and message.startswith(prefix):
        return message[len(prefix):].strip()
    else:
        return "Looks like you’re not authorized to get information about that."

def safeguard_prompt_reply(text, conversation_type, thread_id, vectorDBInfo=None):
    try:
        with trust3_client.create_shield_context(application=trust3_ai_app, username=CURRENT_SNOWFLAKE_USER, 
                                                 use_external_groups=True, user_groups=[CURRENT_SNOWFLAKE_USER_ROLE], 
                                                 vectorDBInfo=vectorDBInfo):
            response = trust3_client.check_access(
                text=text,
                conversation_type=conversation_type,
                thread_id=thread_id
            )
            return True, response[0].response_text

    except paig_client.exception.AccessControlException as e:
        error_message = f"AccessControlException: {e}"
        prefix = "AccessControlException: ERROR: PAIG-400004: "
        if "denied" not in error_message.lower() and error_message.startswith(prefix):
            error_message = error_message[len(prefix):].strip()
        else:
            error_message = "Looks like you’re not authorized to get information about that."
        return False, error_message

def get_trust3_safeguarded_query(query, thread_id):
    # Get safeguarded query from Trust3
    authorized, result = safeguard_prompt_reply(query, ConversationType.PROMPT, thread_id)
    return result

def get_trust3_safeguarded_response(text, sql, citations, thread_id, vectorDBInfo=None):
    # Get safeguarded response from Trust3
    def safeguard(content, vector_db_info):
        if content:
            authorized, result = safeguard_prompt_reply(content, ConversationType.REPLY, thread_id, vector_db_info)
            if not authorized:
                return False, result
            return True, result
        return True, content  # Return as-is if content is None or empty

    inputs = [text, sql]
    safeguarded_outputs = []

    is_vector_db_filter_sent = False
    for content in inputs:
        if content:
            if not is_vector_db_filter_sent:
                authorized, result = safeguard(content, vectorDBInfo)
                is_vector_db_filter_sent = True
            else:
                authorized, result = safeguard(content, None)
            if not authorized:
                return result, None, None
            safeguarded_outputs.append(result)
        else:
            safeguarded_outputs.append(content)

    safeguarded_outputs.append(citations)

    return tuple(safeguarded_outputs)

def audit_sql_dataframe(sql_dataframe, thread_id):
    pd_dataframe = sql_dataframe.to_pandas()
    sql_csv = pd_dataframe.to_csv(index=False)
    authorized, updated_sql_csv = safeguard_prompt_reply(
        text=sql_csv,
        conversation_type=ConversationType.REPLY,
        thread_id=thread_id
    )

def get_trust3_cortex_search_filter(thread_id):
    with trust3_client.create_shield_context(application=trust3_ai_app, username=CURRENT_SNOWFLAKE_USER, use_external_groups=True, user_groups=[CURRENT_SNOWFLAKE_USER_ROLE]):
        filter_response = trust3_client.get_vector_db_filter_expression(
            thread_id=thread_id
        )

        filter_response_trimmed = filter_response.strip()

        filter = None
        vector_db_info = None
        if filter_response_trimmed:
            # By default, filter_response is a string. Convert it to a dictionary if required by your vector database
            filter = ast.literal_eval(filter_response_trimmed)

            # Object needed to pass to next reply safeguard call to audit the cortex search filter
            vector_db_info = trust3_client.get_current("vectorDBInfo")
        
        return filter, vector_db_info

# ================================ END TRUST3 SETUP AND HELPER FUNCTIONS ================================

API_ENDPOINT = "/api/v2/cortex/agent:run"
API_TIMEOUT = 50000  # in milliseconds

CORTEX_SEARCH_SERVICES = "sales_intelligence.data.sales_conversation_search"
SEMANTIC_MODELS = "@sales_intelligence.data.models/sales_metrics_model.yaml"

def run_snowflake_query(query):
    try:
        df = session.sql(query.replace(';',''))
        
        return df

    except Exception as e:
        st.error(f"Error executing SQL: {str(e)}")
        return None, None

def snowflake_api_call(query: str, limit: int = 10, filter: dict = None):
    
    payload = {
        "model": "claude-4-sonnet",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": query
                    }
                ]
            }
        ],
        "tools": [
            {
                "tool_spec": {
                    "type": "cortex_analyst_text_to_sql",
                    "name": "analyst1"
                }
            },
            {
                "tool_spec": {
                    "type": "cortex_search",
                    "name": "search1"
                }
            }
        ],
        "tool_resources": {
            "analyst1": {"semantic_model_file": SEMANTIC_MODELS},
            "search1": {
                "name": CORTEX_SEARCH_SERVICES,
                "max_results": limit,
                "id_column": "conversation_id"
            }
        }
    }

    if filter:
        payload["tool_resources"]["search1"]["filter"] = filter
    
    try:
        resp = _snowflake.send_snow_api_request(
            "POST",  # method
            API_ENDPOINT,  # path
            {},  # headers
            {},  # params
            payload,  # body
            None,  # request_guid
            API_TIMEOUT,  # timeout in milliseconds,
        )
        
        if resp["status"] != 200:
            st.error(f"❌ HTTP Error: {resp['status']} - {resp.get('reason', 'Unknown reason')}")
            st.error(f"Response details: {resp}")
            return None
        
        try:
            response_content = json.loads(resp["content"])
        except json.JSONDecodeError:
            st.error("❌ Failed to parse API response. The server may have returned an invalid JSON format.")
            st.error(f"Raw response: {resp['content'][:200]}...")
            return None
            
        return response_content
            
    except Exception as e:
        st.error(f"Error making request: {str(e)}")
        return None

def process_sse_response(response):
    """Process SSE response"""
    text = ""
    sql = ""
    citations = []
    
    if not response:
        return text, sql, citations
    if isinstance(response, str):
        return text, sql, citations
    try:
        for event in response:
            if event.get('event') == "message.delta":
                data = event.get('data', {})
                delta = data.get('delta', {})
                
                for content_item in delta.get('content', []):
                    content_type = content_item.get('type')
                    if content_type == "tool_results":
                        tool_results = content_item.get('tool_results', {})
                        if 'content' in tool_results:
                            for result in tool_results['content']:
                                if result.get('type') == 'json':
                                    text += result.get('json', {}).get('text', '')
                                    search_results = result.get('json', {}).get('searchResults', [])
                                    for search_result in search_results:
                                        citations.append({'source_id':search_result.get('source_id',''), 'doc_id':search_result.get('doc_id', '')})
                                    sql = result.get('json', {}).get('sql', '')
                    if content_type == 'text':
                        text += content_item.get('text', '')
                            
    except json.JSONDecodeError as e:
        st.error(f"Error processing events: {str(e)}")
                
    except Exception as e:
        st.error(f"Error processing events: {str(e)}")
        
    return text, sql, citations

def safeguarded_transcript_text(citations, thread_id, vector_db_info):
    transcript_text = ""
    for citation in citations:
        doc_id = citation.get("doc_id", "")
        if doc_id:
            query = f"SELECT transcript_text FROM sales_conversations WHERE conversation_id = '{doc_id}'"
            result = run_snowflake_query(query)
            result_df = result.to_pandas()
            if not result_df.empty:
                transcript_text = result_df.iloc[0, 0]
                _, safeguarded_transcript_text = safeguard_prompt_reply(transcript_text, ConversationType.REPLY, thread_id, vector_db_info)
                citation["safeguarded_transcript_text"] = safeguarded_transcript_text
            else:
                transcript_text = "No transcript available"

def main():
    st.title("Intelligent Sales Assistant")

    # Sidebar for new chat
    with st.sidebar:
        if st.button("New Conversation", key="new_chat"):
            st.session_state.messages = []
            st.rerun()

    # Initialize session state
    if 'messages' not in st.session_state:
        st.session_state.messages = []

    for message in st.session_state.messages:
        with st.chat_message(message['role']):
            st.markdown(message['content'].replace("•", "\n\n"))

    if user_query := st.chat_input("Would you like to learn?"):
        # Add user message to chat
        thread_id = get_conversation_thread_id()
        query = get_trust3_safeguarded_query(user_query, thread_id)

        with st.chat_message("user"):
            st.markdown(query)
        st.session_state.messages.append({"role": "user", "content": query})
        
        # Get response from API
        with st.spinner("Processing your request..."):
            cortex_search_filter, vector_db_info = get_trust3_cortex_search_filter(thread_id)
            
            response = snowflake_api_call(query, 1, cortex_search_filter)
            text, sql, citations = process_sse_response(response)

            # Add assistant response to chat
            if text:
                text = text.replace("【†", "[")
                text = text.replace("†】", "]")

                safeguarded_transcript_text(citations, thread_id, vector_db_info)
                
                _, safeguarded_text = safeguard_prompt_reply(text, ConversationType.REPLY, thread_id)
                st.session_state.messages.append({"role": "assistant", "content": safeguarded_text})
                
                with st.chat_message("assistant"):
                    st.markdown(safeguarded_text.replace("•", "\n\n"))
                    if citations:
                        st.write("Citations:")
                        for citation in citations:
                            doc_id = citation.get("doc_id", "")
                            if doc_id and citation.get("safeguarded_transcript_text"):
                                with st.expander(f"[{citation.get('source_id', '')}]"):
                                    st.write(citation.get("safeguarded_transcript_text"))

            # Display SQL if present
            if sql:
                st.markdown("### Generated SQL")

                # Making call here to audit the sql
                safeguard_prompt_reply(sql, ConversationType.REPLY, thread_id)
                
                st.code(sql, language="sql")
                sales_results = run_snowflake_query(sql)
                if sales_results:
                    audit_sql_dataframe(sales_results, thread_id)
                    st.write("### Sales Metrics Report")
                    st.dataframe(sales_results)

if __name__ == "__main__":
    main()
