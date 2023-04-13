#!/usr/bin/env python
#-*- coding: utf-8 -*-
# chenwu@espressif.com

import requests, sys

# GitLab API endpoint
GITLAB_API = 'https://gitlab.espressif.cn:6688/api/v4'

# Personal access token for authentication
gitlab_token_file='/home/chenwu/.gitlab_oauth_token'

# ID of the project containing the pipeline
PROJECT_ID = '234'

# Get pipeline ID from command line arguments
if len(sys.argv) < 2:
    print('Usage: python retry_pipeline.py <pipeline_id>')
    sys.exit(1)

# Function to retry failed jobs in a GitLab pipeline
def retry_failed_jobs(PIPELINE_ID):
    if not PIPELINE_ID.isdigit():
        print('Skip invalid pipeline ID: {}'.format(PIPELINE_ID))
        return

    with open(gitlab_token_file) as f:
        GITLAB_TOKEN = f.read().strip('\n')

    # Construct API URL for getting jobs in the pipeline
    url = f'{GITLAB_API}/projects/{PROJECT_ID}/pipelines/{PIPELINE_ID}/jobs'
    print('url:{}'.format(url))

    # Send GET request to get jobs in the pipeline
    response = requests.get(url, headers={'PRIVATE-TOKEN': GITLAB_TOKEN})
    print('response:{}'.format(response))

    # Check if request was successful
    if response.status_code == 200:
        jobs = response.json()

        # Loop through jobs and retry failed jobs
        for job in jobs:
            # Check if job status is failed
            if job['status'] == 'failed':
                # Construct API URL for retrying the job
                retry_url = f'{GITLAB_API}/projects/{PROJECT_ID}/jobs/{job["id"]}/retry'

                # Send POST request to retry the job
                retry_response = requests.post(retry_url, headers={'PRIVATE-TOKEN': GITLAB_TOKEN})

                # Check if retry request was successful
                if retry_response.status_code == 201:
                    print(f'Successfully retried job "{job["name"]}"')
                else:
                    print(f'Failed to retry job "{job["name"]}". Status code: {retry_response.status_code}')
    else:
        print(f'Failed to get jobs from pipeline. Status code: {response.status_code}')

# Call the function to retry failed jobs in the pipeline
for i in range(1, len(sys.argv)):
    retry_failed_jobs(sys.argv[i])
