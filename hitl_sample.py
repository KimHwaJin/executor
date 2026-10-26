import requests
import json
import time

headers= {
    "Content-Type": "application/json",
    "x-api-key": "" # langflow IDE > 설정 > API key 발급
}

LANGFLOW_SERVER_URL = "" # 예시: http://{프로젝트명}-langflow.aipp02.skhynix.com
FLOW_ID = "" # serer_url 뒤에 /flows/{flow_id} 형태로 붙어있는 flow id


# step1. Initialize and run the workflow

def start_workflow():

    url = f"{LANGFLOW_SERVER_URL}/api/v2/workflows"
    payload = {
        "flow_id": FLOW_ID,
        "input_value":"",
        "session_id": "",
        "mode" : "background"
    }

    response= requests.post(url, headers=headers, json=payload)
    response.raise_for_status()

    job_data= response.json()

    return job_data.get("job_id") or job_data.get("id")

def get_allowed_decisions(job_id):
    url = f"{LANGFLOW_SERVER_URL}/api/v2/workflows/{job_id}/events"
    while True:
        response = requests.get(url, headers=headers)
        rsp_text=response.text
        stripped = rsp_text.split("data:")[-1]
        content = stripped[stripped.find("{"):stripped.rfind("}")+1]
        json_content = json.loads(content.encode().decode("unicode_escape"))
        if json_content.get("data") is not None:
            return json_content

        else:
            print("something wrong...",json_content)
            time.sleep(2)

# step2. monitor for suspended (hitl) state

def wait_for_human_gate(job_id):
    url = f"{LANGFLOW_SERVER_URL}/api/v2/workflows"
    query_params = {
        "job_id": job_id
    }

    while True:
        response = requests.get(url, headers=headers, params=query_params)
        response.raise_for_status()
        status_data=response.json()
        status = status_data.get("status")

        print(f"Current Workflow Status: {status}")

        if status=="suspended":
            print("[hitl alert]: workflow paused at human input checkpoint.")
            return get_allowed_decisions(job_id)
        elif status in ["completed","failed","cancelled"]:
            print(f"Workflow teminated prematurely with status:{status_data}")
            return False

        time.sleep(2)

# step 3. submit the human decision

def submit_human_decision(job_id, request_id, user_action):
    url = f"{LANGFLOW_SERVER_URL}/api/v2/workflows/{job_id}/resume"

    print(f"sending decision: '{user_action}' to resume Workflow..")
    payload = {
        "request_id": request_id,
        "decision": {
            'label': user_action,
            'action_id': user_action
        }
    }
    response = requests.post(url, headers=headers, json=payload)
    response.raise_for_status()

    print("success . workflow has resumed  execution.")
    return response.json()

# Execution Chain

if __name__=="__main__":
    # start execution loop
    active_job_id = start_workflow()
    print(f"Workflow started. Job ID:{active_job_id}")

    # wait until it hits the human gate
    hitl = wait_for_human_gate(active_job_id)
    if hitl:
        print(hitl)
        user_input = input("choose action: ")
        # simulate human clicking 'approve' on your external app interface
        resume_job = submit_human_decision(active_job_id, request_id=hitl['data'])
        print("resume result..", resume_job)
        wait_for_human_gate(job_id=active_job_id)