FROM python

RUN pip3 install mergin-client

COPY ./mergin-py-sync.py ./mergin-py-sync.py

CMD [ "python", "./mergin-py-sync.py" ]
