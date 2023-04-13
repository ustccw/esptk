#!/usr/bin/env python
#-*- coding: utf-8 -*-
# chenwu@espressif.com

import os, sys, requests

# GitLab API endpoint
GITLAB_API = 'https://gitlab.espressif.cn:6688/api/v4'

# Personal access token for authentication
gitlab_token_file = os.path.join(os.path.expanduser('~'), '.gitlab_oauth_token')

# ID of the project containing the pipeline
PROJECT_ID = '234'

# Global variables
total_retry_jobs = 0

def ESP_LOGI(x):
    print('\033[32m{}\033[0m'.format(x))

def ESP_LOGIB(x):
    print('\033[1;32m{}\033[0m'.format(x))

def ESP_LOGE(x):
    print('\033[31m{}\033[0m'.format(x))

def ESP_LOGB(x):
    print('\033[1m{}\033[0m'.format(x))

def ESP_LOGN(x):
    print('{}'.format(x))

def ESP_LOGW(x):
    print('\033[33m{}\033[0m'.format(x))

def ESP_LOGWB(x):
    print('\033[1;33m{}\033[0m'.format(x))

# Get pipeline ID from command line arguments
if len(sys.argv) < 2:
    ESP_LOGE('Usage: python retry_pipeline.py <pipeline_id>')
    sys.exit(1)

# Function to retry failed jobs in a GitLab pipeline
def retry_failed_jobs(PIPELINE_ID):
    if not PIPELINE_ID.isdigit():
        ESP_LOGW('Skip invalid pipeline ID: {}'.format(PIPELINE_ID))
        return

    with open(gitlab_token_file, 'r') as f:
        GITLAB_TOKEN = f.read().strip()

    # Construct API URL for getting jobs in the pipeline
    url = f'{GITLAB_API}/projects/{PROJECT_ID}/pipelines/{PIPELINE_ID}/jobs'
    ESP_LOGN('URL -> {}'.format(url))

    # Send GET request to get jobs in the pipeline
    # Note: We are using the per_page and page parameters to get all jobs in the pipeline
    # If there are more than 100 jobs in the pipeline, we need to loop through all pages
    response = requests.get(url, params={'per_page': 100, 'page': 1}, headers={'PRIVATE-TOKEN': GITLAB_TOKEN})

    # Check if request was successful
    if response.status_code == 200:
        jobs = response.json()

        # Loop through jobs and retry failed jobs
        variables = ['total_jobs', 'running_jobs', 'failed_jobs', 'success_jobs', 'other_jobs']
        pipeline_stat = {var: 0 for var in variables}
        for job in jobs:
            pipeline_stat['total_jobs'] += 1
            # Check if job status is failed
            if job['status'] == 'failed':
                pipeline_stat['failed_jobs'] += 1
                # Construct API URL for retrying the job
                retry_url = f'{GITLAB_API}/projects/{PROJECT_ID}/jobs/{job["id"]}/retry'

                # Send POST request to retry the job
                retry_response = requests.post(retry_url, headers={'PRIVATE-TOKEN': GITLAB_TOKEN})

                # Check if retry request was successful
                if retry_response.status_code == 201:
                    ESP_LOGI(f'Successfully retried job "{job["name"]}"')
                    global total_retry_jobs
                    total_retry_jobs += 1
                else:
                    ESP_LOGE(f'Failed to retry job "{job["name"]}". Status code: {retry_response.status_code}')
            elif job['status'] == 'running':
                pipeline_stat['running_jobs'] += 1
            elif job['status'] == 'success':
                pipeline_stat['success_jobs'] += 1
            else:
                pipeline_stat['other_jobs'] += 1
        ESP_LOGI('Pipeline statistics ----> total jobs: {}, running jobs: {}, failed jobs: {}, success jobs: {}, others jobs: {}\r\n'.format(
            pipeline_stat['total_jobs'], pipeline_stat['running_jobs'], pipeline_stat['failed_jobs'], pipeline_stat['success_jobs'], pipeline_stat['other_jobs']))
    else:
        ESP_LOGE(f'Failed to get jobs from pipeline. Status code: {response.status_code}')

# Call the function to retry failed jobs in the pipeline
for i in range(1, len(sys.argv)):
    retry_failed_jobs(sys.argv[i])

ESP_LOGIB('Total retried jobs: {}'.format(total_retry_jobs))
