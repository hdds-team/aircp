#!/bin/sh

AGENT=$1
if [ "${AGENT}" = "" ]; then
 exit 0
fi

if [ "${AGENT}" = "all" ]; then
 ps aux | grep python | grep heartbeat.py| awk {'print $2'} |while read a; do kill -9 $a; done 
else
 PID=$(ps aux | grep python | grep heartbeat.py| grep "${AGENT}" | awk {'print $2'})
 echo "PID: $PID"
 kill -15 ${PID}
fi

# pkill -9 -f mcp-server ; pkill -9 -f devitd
