## E1
| system | attempt | calls | calls_with_leak | distinct_leaked_canaries | responses | writes_accepted | foreign_tag_results | own_twin_found | own_twin_expected | foreign_content_returned | spoofed_record_visible |
|---|---|---|---|---|---|---|---|---|---|---|---|
| team-server | 1-direct-canary | 360 | 0 | 0 | {"200:None": 360} |  |  |  |  |  |  |
| team-server | 1-direct-text | 360 | 0 | 0 | {"200:None": 360} |  |  |  |  |  |  |
| team-server | 1-personal-text | 132 | 0 | 0 | {"200:None": 132} |  |  |  |  |  |  |
| team-server | 2-paraphrase | 360 | 0 | 0 | {"200:None": 360} |  |  |  |  |  |  |
| team-server | 3-twin | 72 | 0 | 0 | {"200:None": 72} |  |  | 36 | 36 |  |  |
| team-server | 4-foreign-team-id | 252 | 0 | 0 | {"400:forbidden": 252} | 0 |  |  |  |  |  |
| team-server | 5-tag-field | 12 | 0 | 0 | {"400:invalid_request": 12} |  |  |  |  |  |  |
| team-server | 5-tag-query | 24 | 0 | 0 | {"200:None": 24} |  |  |  |  |  |  |
| team-server | 5-tag-save | 96 | 0 | 0 | {"400:invalid_request": 96} |  |  |  |  |  |  |
| team-server | 6-reader-write | 12 | 0 | 0 | {"400:forbidden": 12} | 0 |  |  |  |  |  |
| team-server | 7-revoked-token | 48 | 0 | 0 | {"401:None": 48} |  |  |  |  |  |  |
| team-server | 7-revoked-token-control | 12 | 0 | 0 | {"200:None": 12} |  |  |  |  |  |  |
| team-server | 8-removed-member | 84 | 0 | 0 | {"200:None": 80, "400:forbidden": 4} |  |  |  |  |  |  |
| team-server | 9-foreign-id-own-scope | 720 | 0 | 0 | {"200:None": 562, "400:conflict": 158} |  |  |  |  | 0 |  |

control: {"questions": 1200, "gold_in_top5": 0.995, "gold_at_1": 0.98, "any_own_canary_in_top5": 1.0, "gold_in_top5_by_role": {"editor": 0.995, "reader": 0.995}}

## E2a
| variant | questions | hit@1 | hit@5 | mrr@10 | fresh@5 | stale@5 | fresh_share_of_found | by_kind_hit@5 | non_team_results_in_top5 | hit@5_by_department |
|---|---|---|---|---|---|---|---|---|---|---|
| A-not-member | 400 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | 0.000 | {"direct": 0.0, "paraphrase": 0.0} | 5.000 | {"engineering": 0.0, "finance": 0.0, "hr": 0.0, "sales": 0.0} |
| B-reader | 400 | 0.980 | 1.000 | 0.986 | 1.000 | 0.000 | 1.000 | {"direct": 1.0, "paraphrase": 1.0} | 2.092 | {"engineering": 1.0, "finance": 1.0, "hr": 1.0, "sales": 1.0} |
| C-team-only | 400 | 0.980 | 1.000 | 0.989 | 1.000 | 0.000 | 1.000 | {"direct": 1.0, "paraphrase": 1.0} | 0.000 | {"engineering": 1.0, "finance": 1.0, "hr": 1.0, "sales": 1.0} |

## E2
```json
{
 "transfer_sequential": {
  "requests": 50,
  "first_request_ms_after_change": 1.637332999962382,
  "requests_with_engineering_data": 0,
  "first_request_leaked": false,
  "sales_visible_in": 50
 },
 "transfer_exact_tools": [
  {
   "op": "memory_get",
   "scope": "engineering",
   "status": 400,
   "code": "forbidden"
  },
  {
   "op": "memory_export",
   "scope": "engineering",
   "status": 400,
   "code": "forbidden"
  },
  {
   "op": "memory_save",
   "scope": "engineering",
   "status": 400,
   "code": "forbidden"
  },
  {
   "op": "memory_save",
   "scope": "sales",
   "status": 200,
   "code": null
  }
 ],
 "transfer_concurrent": {
  "requests": 174,
  "not_overlapping_change": 114,
  "not_overlapping_with_old_access": 0,
  "overlapping_change": 60,
  "overlapping_rejected": 60,
  "overlapping_with_team_not_held_at_send": 0,
  "strict_leaks": 0,
  "codes": {
   "200:None": 114,
   "400:forbidden": 60
  },
  "latency_p50_ms": 672.1279590019549
 },
 "write_race": {
  "attempts": 40,
  "committed": 15,
  "client_error": 25,
  "error_but_committed": 0,
  "codes": {
   "400:forbidden": 25,
   "200:None": 15
  }
 },
 "offboarding": [
  {
   "check": "leaver-request",
   "error": "Invalid or revoked token",
   "experiment": "E2-offboarding",
   "http_status": 401,
   "op": "memory_scopes"
  },
  {
   "check": "leaver-request",
   "error": "Invalid or revoked token",
   "experiment": "E2-offboarding",
   "http_status": 401,
   "op": "memory_recall"
  },
  {
   "check": "leaver-request",
   "error": "Invalid or revoked token",
   "experiment": "E2-offboarding",
   "http_status": 401,
   "op": "memory_get"
  },
  {
   "check": "leaver-request",
   "error": "Invalid or revoked token",
   "experiment": "E2-offboarding",
   "http_status": 401,
   "op": "memory_save"
  },
  {
   "check": "colleague-access",
   "colleague": "sales-editor2",
   "created_by_preserved": 25,
   "experiment": "E2-offboarding",
   "leaver_records": 25,
   "visible": 25
  },
  {
   "check": "colleague-access",
   "colleague": "sales-reader",
   "created_by_preserved": 25,
   "experiment": "E2-offboarding",
   "leaver_records": 25,
   "visible": 25
  },
  {
   "check": "personal-search-by-other",
   "experiment": "E2-offboarding",
   "foreign_scopes": [],
   "http_status": 200,
   "leaked_canaries": [],
   "own_canaries": [
    "CANARY-sales-1200",
    "CANARY-sales-9599"
   ],
   "user": "sales-editor2"
  },
  {
   "check": "personal-owner-id-scope",
   "error": "1 validation error for RecordRequest\nscope.owner_id\n  Extra inputs are not permitted [type=extra_forbidden, input_value='sales-editor1', input_type=str]\n    For",
   "experiment": "E2-offboarding",
   "http_status": 400,
   "user": "sales-editor2"
  },
  {
   "check": "personal-search-by-other",
   "experiment": "E2-offboarding",
   "foreign_scopes": [],
   "http_status": 200,
   "leaked_canaries": [],
   "own_canaries": [
    "CANARY-engineering-7257"
   ],
   "user": "engineering-editor1"
  },
  {
   "check": "personal-owner-id-scope",
   "error": "1 validation error for RecordRequest\nscope.owner_id\n  Extra inputs are not permitted [type=extra_forbidden, input_value='sales-editor1', input_type=str]\n    For",
   "experiment": "E2-offboarding",
   "http_status": 400,
   "user": "engineering-editor1"
  }
 ],
 "notes": {
  "personal_after_offboarding": {
   "workspace_dir_exists": true,
   "active_records_on_disk": 3,
   "users.active": 1,
   "unrevoked_tokens": 0,
   "admin_events": [
    [
     "user_created",
     "sales-editor1"
    ],
    [
     "membership:editor",
     "sales-editor1:sales"
    ],
    [
     "token_created",
     "sales-editor1:orgbench"
    ],
    [
     "membership:None",
     "sales-editor1:sales"
    ]
   ]
  },
  "personal_after_token_reissue": {
   "http_status": 200,
   "own_note_found": true
  },
  "user_disable": {
   "available": true,
   "leaver_http_statuses": [
    401,
    401,
    401
   ],
   "token_reissue_exit_code": 1
  },
  "backup_contains_leaver_personal": true,
  "cli_commands": "user-add,team-add,member,token-create,user-disable,token-revoke,backup,restore,serve"
 }
}
```

## E3
| clients | rounds | rounds_exactly_one_success | successes | conflicts | other_errors | lost_updates | latency_p50_ms | latency_p95_ms | latency_max_ms |
|---|---|---|---|---|---|---|---|---|---|
| 2 | 100 | 100 | 100 | 100 | 0 | 0 | 17.3 | 25.9 | 32.8 |
| 4 | 100 | 100 | 100 | 300 | 0 | 0 | 17.7 | 27.2 | 32.1 |
| 8 | 100 | 100 | 100 | 700 | 0 | 0 | 20.4 | 33.6 | 42.6 |
| 16 | 100 | 100 | 100 | 1500 | 0 | 0 | 27.6 | 42.8 | 64.3 |

| timeout | op | reps | first_attempt_timed_out | timed_out_but_committed | exactly_one_effect | retry_same_result | different_payload_rejected |
|---|---|---|---|---|---|---|---|
| client | memory_delete | 20 | 20 | 0 | 20 | 20 | 20 |
| client | memory_save | 20 | 20 | 0 | 20 | 20 | 20 |
| client | memory_update | 20 | 20 | 0 | 20 | 20 | 20 |
| server | memory_delete | 20 | 0 | 0 | 20 | 20 | 20 |
| server | memory_save | 20 | 20 | 0 | 20 | 20 | 20 |
| server | memory_update | 20 | 20 | 0 | 20 | 20 | 20 |

```json
{
 "history": [
  {
   "clients": 2,
   "distinct_actors": 2,
   "every_success_in_history": true,
   "experiment": "E3-history",
   "final_id": 101,
   "final_revision_chain_length": 101,
   "first_id": 1,
   "history_events": 201,
   "insert_events": 101,
   "rounds": 100,
   "successful_updates": 100,
   "supersede_events": 100
  },
  {
   "clients": 4,
   "distinct_actors": 4,
   "every_success_in_history": true,
   "experiment": "E3-history",
   "final_id": 202,
   "final_revision_chain_length": 101,
   "first_id": 102,
   "history_events": 201,
   "insert_events": 101,
   "rounds": 100,
   "successful_updates": 100,
   "supersede_events": 100
  },
  {
   "clients": 8,
   "distinct_actors": 8,
   "every_success_in_history": true,
   "experiment": "E3-history",
   "final_id": 303,
   "final_revision_chain_length": 101,
   "first_id": 203,
   "history_events": 201,
   "insert_events": 101,
   "rounds": 100,
   "successful_updates": 100,
   "supersede_events": 100
  },
  {
   "clients": 16,
   "distinct_actors": 15,
   "every_success_in_history": true,
   "experiment": "E3-history",
   "final_id": 404,
   "final_revision_chain_length": 101,
   "first_id": 304,
   "history_events": 201,
   "insert_events": 101,
   "rounds": 100,
   "successful_updates": 100,
   "supersede_events": 100
  }
 ],
 "cross_scope": [
  {
   "different_payload_other_scope": {
    "accepted": true,
    "second_id": 1
   },
   "experiment": "E3-retry-cross-scope",
   "first_id": 1,
   "same_payload_other_scope": {
    "accepted": true,
    "third_id": 82
   }
  }
 ]
}
```
