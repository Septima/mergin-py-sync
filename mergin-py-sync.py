import os
import mergin

url = os.getenv('MERGIN_URL')
login = os.getenv('MERGIN_USERNAME')
password = os.getenv('MERGIN_PASSWORD')
data = os.getenv('MERGIN_DATA')

client = mergin.MerginClient(url=url, login=login, password=password)
projects = client.projects_list()
for project in projects:
    project_path = os.path.join(project['namespace'], project['name'])
    dest_path = os.path.join(data, project_path)
    if os.path.exists(dest_path):
        client.pull_project(dest_path)
    else:
        client.download_project(project_path=project_path, directory=dest_path)
