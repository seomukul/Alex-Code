
import json
import requests
from apiclient import errors
from apiclient.discovery import build

import datetime
import dateutil

from google.oauth2 import service_account
import google.oauth2.credentials
import google.auth.transport.requests
from google.cloud import bigquery

import pandas as pd

import dateutil
import pandas_gbq


import functions_framework

@functions_framework.http
def run(request):
    """Responds to any HTTP request.
    Args:
        request (flask.Request): HTTP request object.
    Returns:
        The response text or any set of values that can be turned into a
        Response object using
        `make_response <http://flask.pocoo.org/docs/1.0/api/#flask.Flask.make_response>`.
    """
    request_json = request.get_json()
    
    ##### get variables
    site = request_json.get('site')
    BQ_DATASET_NAME = request_json.get('BQ_DATASET_NAME')
    BQ_TABLE_NAME = request_json.get('BQ_TABLE_NAME')
    BQ_PROJECT_NAME = request_json.get('BQ_PROJECT_NAME')

    # optional defaults to 4 days ago
    n_days_ago = request_json.get('n_days_ago', 4)

    # optional
    page_url = request_json.get('page_url', None)
    # end Date is start_date+1

    # optional start_date
    if request_json.get('start_date'):
        # given a start date so use that
        start_date = datetime.datetime.strptime( request_json.get('start_date'), '%Y-%m-%d')
    else:
        # defaults to today minus n_days_ago
        start_date = (datetime.datetime.today() - datetime.timedelta(days=n_days_ago))
    
    # optional end_date
    if request_json.get('end_date'):
        # given a start date so use that
        end_date = datetime.datetime.strptime( request_json.get('end_date'), '%Y-%m-%d')
    else:
        end_date = start_date

    # optional n_days_back
    n_days_back = request_json.get('n_days_back', 1)




    ##### uses the locally uploaded service account key
    SERVICE_ACCOUNT_FILE = "./service-account-key.json"
    SCOPES = [
      'https://www.googleapis.com/auth/webmasters',
      'https://www.googleapis.com/auth/bigquery'
    ]
    credentials = service_account.Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    

    output = {}

    start_date_input = start_date
    end_date_input = end_date

    # loop over the days back
    for nth_day_ago in range(0,n_days_back):

        start_date = (start_date_input - datetime.timedelta(days=nth_day_ago))
        end_date = (end_date_input - datetime.timedelta(days=nth_day_ago))

        #### Google Search Console
        # initiates the credentials
        service = build(
            'webmasters',
            'v3',
            credentials=credentials
        )
        response = service.sites().list().execute()

        # initialize for raw rows
        all_rows_as_json = []
        # initialize the dataset for formatted rows
        df_all_queries = pd.DataFrame()

        # recent daily pull query for all urls
        data = {
        "startDate": start_date.strftime("%Y-%m-%d"),
        "endDate": end_date.strftime("%Y-%m-%d"),
        "dimensions": ["date","page","query","device","country"],
        "rowLimit": 25000
        }

        # if a page_url is given
        if page_url:
            data["dimensionFilterGroups"] = [
                {
                    "groupType": "and",
                    "filters": [
                    {
                        "dimension": "page",
                        "operator": "contains",
                        "expression": page_url
                    }
                    ]
                }
                ]

        # Assign Start Row
        start_row = 0
        # Loop over requests until all rows are pulled into DataFrame
        while True:
            data['startRow'] = start_row
            res = requests.post("https://www.googleapis.com/webmasters/v3/sites/"+"sc-domain:"+site+"/searchAnalytics/query?access_token="+credentials.token, json=data)

            # convert the response to a data frame
            j = json.loads(res.text).get('rows',[])

            if j:
                all_rows_as_json.extend(j)
            else:
                break
            if len(j) < 25000:
                break

            start_row += 25000


        # convert the raw rows to a pandas df
        if(len(all_rows_as_json)):
            df_queries = pd.DataFrame(j)
            df_queries['property'] = site
            df_queries['updated_at'] = datetime.datetime.now()

            # By default the keys/dimensions are in a single column, let's split them out into separate columns.
            new_cols = pd.DataFrame()
            # Bring back a key from the intial dataframe so we can join
            new_cols['key'] = df_queries['keys']
            new_cols[['start_date', "url", "query", "device", "country"]] = pd.DataFrame(df_queries['keys'].tolist(), index= df_queries.index)
            
            # convert string to date
            new_cols["start_date"] =  pd.to_datetime(new_cols["start_date"], format="%Y-%m-%d").dt.date

            # Join in the new clean columns to our intiial data
            df_queries = pd.concat([df_queries, new_cols], axis=1, join='inner')

            # Drop the key columns
            df_queries = df_queries.drop(["key","keys","ctr"],axis=1)

            # save all the queries for this page with all other pages
            df_all_queries = pd.concat([df_all_queries, df_queries])


            ##### Bigquery
            # establish a BigQuery client
            client_bq = bigquery.Client.from_service_account_json(SERVICE_ACCOUNT_FILE)
            BQ_PROJECT_NAME = client_bq.project


            # verify there are no duplicate entries in bigquery
            # Query bigquery to determine what has already been loaded?
            # Update the in-memory credentials cache (added in pandas-gbq 0.7.0).
            pandas_gbq.context.credentials = credentials
            pandas_gbq.context.project = BQ_PROJECT_NAME


            def query_table(start_date, end_date, PROJECT_ID, DATASET, TABLE):
                QUERY = "SELECT * FROM {dataset}.{table} WHERE start_date >= '{start_date}' and start_date <= '{end_date}'"
                
                query = QUERY.format(dataset=DATASET, table=TABLE, start_date=start_date, end_date=end_date)
                result = pd.read_gbq(query, PROJECT_ID, dialect='standard')

                result['already_loaded'] = True
                return result


            # get all the records already loaded within the last n days
            already_loaded = query_table(start_date.strftime("%Y-%m-%d"),end_date.strftime("%Y-%m-%d"), BQ_PROJECT_NAME, BQ_DATASET_NAME, BQ_TABLE_NAME)

            # find the rows that aren't loaded yet
            join_cols = ['start_date','property','url','device','country']
            not_loaded = df_all_queries.merge(already_loaded[join_cols+['already_loaded']], how="left", on=join_cols)
            not_loaded = not_loaded[not_loaded['already_loaded'].isna()]
            cols = list(df_all_queries.columns)
            not_loaded = not_loaded[cols]

            # record n_records_loaded
            n_records_loaded = len(not_loaded)
            output["n_records_loaded"] = output.get("n_records_loaded",0) + n_records_loaded
            output["n_days_back_loaded"] = output.get("n_days_back_loaded",0) + 1
            if output.get('start_date') is None:
                output["start_date"] = str(start_date.date())

            # continue loading if not loaded yet
            if n_records_loaded:

                # create a job config
                # Set the destination table
                
                table_id = '{}.{}.{}'.format(BQ_PROJECT_NAME, BQ_DATASET_NAME, BQ_TABLE_NAME)
                job_config = bigquery.LoadJobConfig(
                    # Specify a (partial) schema. All columns are always written to the
                    # table. The schema is used to assist in data type definitions.
                    schema=[
                        # Specify the type of columns whose type cannot be auto-detected. For
                        # example the "title" column uses pandas dtype "object", so its
                        # data type is ambiguous.
                        #bigquery.SchemaField("title", bigquery.enums.SqlTypeNames.STRING),
                        # Indexes are written if included in the schema by name.
                        #bigquery.SchemaField("wikidata_id", bigquery.enums.SqlTypeNames.STRING),
                    ],
                    # Optionally, set the write disposition. BigQuery appends loaded rows
                    # to an existing table by default, but with WRITE_TRUNCATE write
                    # disposition it replaces the table with the loaded data.
                    write_disposition="WRITE_APPEND",
                )

                job = client_bq.load_table_from_dataframe(
                    not_loaded, table_id, job_config=job_config
                )  # Make an API request.
                job.result()  # Wait for the job to complete.

        # end nth day

    # finish
    return output