## E1
| system | attempt | calls | calls_with_leak | distinct_leaked_canaries | responses | writes_accepted | foreign_tag_results | own_twin_found | own_twin_expected | foreign_content_returned | spoofed_record_visible |
|---|---|---|---|---|---|---|---|---|---|---|---|
| local-single-store/client-filter | 1-direct-canary | 360 | 0 | 0 | {"None:None": 360} |  | 0 |  |  |  |  |
| local-single-store/client-filter | 1-direct-text | 360 | 0 | 0 | {"None:None": 360} |  | 0 |  |  |  |  |
| local-single-store/client-filter | 2-paraphrase | 360 | 0 | 0 | {"None:None": 360} |  | 0 |  |  |  |  |
| local-single-store/client-filter | 3-twin | 72 | 0 | 0 | {"None:None": 72} |  | 0 |  |  |  |  |
| local-single-store/client-filter | 5-tag-query | 24 | 0 | 0 | {"None:None": 24} |  | 0 |  |  |  |  |
| local-single-store/client-filter | 5-tag-spoof-write | 12 | 0 | 0 | {"None:None": 12} |  |  |  |  |  | 3 |
| local-single-store/no-filter | 1-direct-canary | 360 | 360 | 187 | {"None:None": 360} |  | 3386 |  |  |  |  |
| local-single-store/no-filter | 1-direct-text | 360 | 360 | 200 | {"None:None": 360} |  | 3472 |  |  |  |  |
| local-single-store/no-filter | 2-paraphrase | 360 | 360 | 200 | {"None:None": 360} |  | 3493 |  |  |  |  |
| local-single-store/no-filter | 3-twin | 72 | 45 | 6 | {"None:None": 72} |  | 387 |  |  |  |  |
| local-single-store/no-filter | 5-tag-query | 24 | 24 | 76 | {"None:None": 24} |  | 239 |  |  |  |  |
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
  "first_request_ms_after_change": 2.7770839988079388,
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
  "requests": 180,
  "not_overlapping_change": 120,
  "not_overlapping_with_old_access": 0,
  "overlapping_change": 60,
  "overlapping_rejected": 60,
  "overlapping_with_team_not_held_at_send": 0,
  "strict_leaks": 0,
  "codes": {
   "200:None": 120,
   "400:forbidden": 60
  },
  "latency_p50_ms": 647.9676669987384
 },
 "write_race": {
  "attempts": 40,
  "committed": 16,
  "client_error": 40,
  "error_but_committed": 16,
  "codes": {
   "400:forbidden": 40
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
   "available": false,
   "without_token_file_only_member_remove_possible": true,
   "shared_write_after_member_remove_status": 200,
   "control_status": 200
  },
  "backup_contains_leaver_personal": true,
  "cli_commands": "user-add,team-add,member,token-create,token-revoke,backup,restore,serve"
 }
}
```

## E3
| clients | rounds | rounds_exactly_one_success | successes | conflicts | other_errors | lost_updates | latency_p50_ms | latency_p95_ms | latency_max_ms |
|---|---|---|---|---|---|---|---|---|---|
| 2 | 100 | 100 | 100 | 100 | 0 | 0 | 17.0 | 23.9 | 29.6 |
| 4 | 100 | 100 | 100 | 300 | 0 | 0 | 17.4 | 26.4 | 29.4 |
| 8 | 100 | 100 | 100 | 700 | 0 | 0 | 19.4 | 30.8 | 33.5 |
| 16 | 100 | 100 | 100 | 1500 | 0 | 0 | 25.5 | 37.8 | 50.4 |

| timeout | op | reps | first_attempt_timed_out | timed_out_but_committed | exactly_one_effect | retry_same_result | different_payload_rejected |
|---|---|---|---|---|---|---|---|
| client | memory_delete | 20 | 20 | 0 | 20 | 20 | 20 |
| client | memory_save | 20 | 20 | 0 | 20 | 20 | 20 |
| client | memory_update | 20 | 20 | 0 | 20 | 20 | 20 |
| server | memory_delete | 20 | 0 | 0 | 20 | 20 | 20 |
| server | memory_save | 20 | 20 | 1 | 20 | 20 | 20 |
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
   "distinct_actors": 16,
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

## E4
| system | scopes | workers | records | cold_first_query_ms | warm_p50_ms | warm_p95_ms | worker_rss_sum_mib | gateway_rss_mib | worker_processes_started | worker_cpu_s_total | worker_cpu_s_warm_batch | load1_before | load1_after |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| team-server | 1 | 1 | 50 | 904.1 | 525.8 | 555.6 | 1309.5 | 37.1 | 1 | 91.0 | 90.1 | [5.2, 4.85] | [6.42, 5.58] |
| single-store | 1 | 1 | 50 | 975.0 | 539.9 | 628.2 | 1308.2 | 37.1 | 1 | 93.7 | 92.7 | [5.8, 5.2] | [6.72, 6.59] |
| team-server | 2 | 1 | 23 | 1824.3 | 1107.2 | 1306.7 | 869.8 | 37.1 | 31 | 49.0 | 47.1 | [5.1, 5.39] | [5.26, 5.2] |
| team-server | 2 | 2 | 23 | 1938.2 | 212.7 | 249.4 | 1942.4 | 37.1 | 2 | 35.8 | 33.7 | [5.26, 4.68] | [5.8, 5.39] |
| single-store | 2 | 1 | 23 | 921.0 | 183.1 | 199.8 | 999.3 | 37.1 | 1 | 31.1 | 30.1 | [5.8, 5.58] | [6.87, 6.02] |
| team-server | 3 | 1 | 73 | 2841.1 | 2307.1 | 2676.0 | 872.2 | 37.2 | 31 | 122.9 | 120.0 | [5.4, 5.52] | [4.9, 5.58] |
| team-server | 3 | 2 | 73 | 2935.2 | 1249.8 | 1773.3 | 1860.9 | 37.2 | 32 | 110.9 | 107.7 | [4.9, 5.06] | [7.41, 5.52] |
| team-server | 3 | 3 | 73 | 2863.8 | 755.8 | 831.3 | 3274.9 | 37.2 | 3 | 128.6 | 125.5 | [5.58, 6.0] | [7.21, 6.58] |
| single-store | 3 | 1 | 73 | 924.0 | 534.8 | 572.3 | 1307.0 | 37.2 | 1 | 91.2 | 90.2 | [4.92, 4.99] | [5.59, 6.0] |
| team-server | 5 | 1 | 173 | 4711.9 | 4122.6 | 4379.6 | 871.9 | 37.2 | 31 | 171.6 | 166.8 | [5.59, 5.18] | [5.35, 6.19] |
| team-server | 5 | 2 | 173 | 4619.9 | 3661.7 | 3927.3 | 1858.0 | 37.2 | 62 | 233.5 | 228.4 | [5.35, 5.59] | [6.54, 5.18] |
| team-server | 5 | 3 | 173 | 4890.5 | 2714.2 | 2961.0 | 2857.5 | 37.2 | 63 | 214.1 | 208.7 | [5.33, 5.02] | [5.44, 5.59] |
| team-server | 5 | 5 | 173 | 4508.1 | 1577.1 | 1694.1 | 5568.4 | 37.3 | 5 | 270.8 | 265.8 | [5.44, 5.44] | [7.53, 7.79] |
| single-store | 5 | 1 | 173 | 918.7 | 512.9 | 555.1 | 1310.7 | 37.2 | 1 | 84.6 | 83.6 | [5.82, 4.1] | [5.96, 5.44] |
| team-server | 8 | 1 | 323 | 7382.4 | 6834.9 | 7245.9 | 884.7 | 37.6 | 31 | 256.2 | 248.8 | [5.96, 5.7] | [4.38, 4.1] |
| team-server | 8 | 2 | 323 | 7590.4 | 6414.5 | 6785.7 | 1854.7 | 37.6 | 62 | 327.9 | 319.5 | [4.38, 5.6] | [5.15, 5.7] |
| team-server | 8 | 3 | 323 | 7501.7 | 5849.5 | 6218.1 | 2858.2 | 37.6 | 93 | 366.3 | 358.0 | [5.15, 5.58] | [5.99, 6.33] |
| team-server | 8 | 8 | 323 | 7095.7 | 2938.1 | 3274.3 | 9035.3 | 37.6 | 8 | 506.2 | 498.3 | [5.99, 5.42] | [9.66, 9.49] |
| single-store | 8 | 1 | 323 | 910.1 | 459.2 | 537.8 | 1289.7 | 37.6 | 1 | 81.7 | 80.7 | [5.59, 4.82] | [6.27, 6.18] |
