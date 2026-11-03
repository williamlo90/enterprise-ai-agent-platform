"""Disposable DynamoDB spike store; no platform tenants or sessions."""

import re

import boto3
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.config import Config
from botocore.exceptions import ClientError


class StoreRejected(RuntimeError):
    def __init__(self):
        super().__init__("Spike operation rejected.")


class DynamoSpikeStore:
    def __init__(self, table_name: str, *, client=None, deadline=None):
        if not re.fullmatch(r"eai-oidc-spike-[a-z0-9-]{1,64}", table_name):
            raise StoreRejected()
        self.table = table_name
        self.client, self.deadline = client, deadline

    def call(self, operation, **values):
        left = self.deadline.remaining(reserve=1) if self.deadline else 4
        if self.client is not None:
            result = getattr(self.client, operation)(**values)
        else:
            with boto3.client("dynamodb", region_name="ap-southeast-1", config=Config(
                connect_timeout=min(1, left / 2), read_timeout=min(1, left / 2),
                retries={"total_max_attempts": 1},
            )) as client:
                result = getattr(client, operation)(**values)
        if self.deadline:
            self.deadline.remaining(reserve=1)
        return result

    def limit(self, now: int):
        values = {":window": {"N": str(now // 60)}, ":one": {"N": "1"}, ":max": {"N": "10"}}
        common = dict(TableName=self.table, Key={"pk": {"S": "rate"}},
                      ExpressionAttributeNames={"#w": "window", "#c": "requests"})
        try:
            self.call("update_item", **common, UpdateExpression="SET #w=:window, #c=:one",
                                    ConditionExpression="attribute_not_exists(pk) OR #w < :window",
                                    ExpressionAttributeValues={k: values[k] for k in (":window", ":one")})
            return
        except ClientError as error:
            if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise StoreRejected() from None
        try:
            self.call("update_item", **common, UpdateExpression="ADD #c :one",
                                    ConditionExpression="#w=:window AND #c < :max",
                                    ExpressionAttributeValues=values)
        except Exception:
            raise StoreRejected() from None

    def start(self, row: dict, old_key: str | None):
        try:
            transaction = [
                {"Update": {"TableName": self.table, "Key": {"pk": {"S": "budget"}},
                            "UpdateExpression": "ADD launches :one",
                            "ConditionExpression": "attribute_not_exists(launches) OR launches < :max",
                            "ExpressionAttributeValues": {":one": {"N": "1"}, ":max": {"N": "100"}}}},
                {"Put": {"TableName": self.table, "Item": {k: TypeSerializer().serialize(v) for k, v in row.items()},
                         "ConditionExpression": "attribute_not_exists(pk)"}},
            ]
            if old_key and old_key != row["pk"]:
                transaction.append({"Delete": {"TableName": self.table, "Key": {"pk": {"S": old_key}}}})
            self.call("transact_write_items", TransactItems=transaction)
        except Exception:
            raise StoreRejected() from None

    def claim(self, key: str, state_hash: str, now: int):
        try:
            response = self.call("update_item",
                TableName=self.table, Key={"pk": {"S": key}},
                UpdateExpression="SET #s=:used REMOVE verifier",
                ConditionExpression="#s=:pending AND state_hash=:state AND expires_at > :now AND attribute_exists(verifier)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":used": {"S": "consumed"}, ":pending": {"S": "pending"},
                                           ":state": {"S": state_hash}, ":now": {"N": str(now)}},
                ReturnValues="ALL_OLD",
            )
            return {k: TypeDeserializer().deserialize(v) for k, v in response["Attributes"].items()}
        except Exception:
            # An ambiguous result never permits code exchange or retry of this claim.
            raise StoreRejected() from None

    def finish(self, key: str, success: bool):
        try:
            self.call("update_item",
                TableName=self.table, Key={"pk": {"S": key}},
                UpdateExpression="SET #s=:result", ConditionExpression="#s=:used",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":result": {"S": "verified" if success else "failed"},
                                           ":used": {"S": "consumed"}},
            )
        except Exception:
            raise StoreRejected() from None

    def result(self, key: str, now: int):
        try:
            response = self.call("get_item", TableName=self.table, Key={"pk": {"S": key}}, ConsistentRead=True)
            row = {k: TypeDeserializer().deserialize(v) for k, v in response.get("Item", {}).items()}
            if not row or row["expires_at"] <= now:
                raise StoreRejected()
            return row["status"] == "verified"
        except Exception:
            raise StoreRejected() from None
