#!/bin/sh
trap 'exit 0' INT TERM
while hdc shell uitest uiInput dircFling 2; do
  sleep 1
done
