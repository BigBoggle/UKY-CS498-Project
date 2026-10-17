import sqlite3
import json


# TODO: create functions to create database from json input.
class RacerDB:
    con = None  # Database Connection

    def __init__(self, db_name: str):
        try:
            self.con = sqlite3.connect(db_name)
        except:
            print(f"Failed to connect to {db_name}")

    # Get access to database connection
    def get_db(self):
        return con

    # Print Database contents in a formatted report to stdout
    def print_db(self):
        if self.con is not None:
            print("DATABASE:")
