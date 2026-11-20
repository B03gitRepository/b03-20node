"""Explicit identity for the RSS no-movement development capability."""
PROFILE_ID='b03-offline-flow-rss-nomove-v1'
SEMANTICS_ID='b03.flow-qos-rss-nomove.v1'
CAPABILITY_ID='b03.train.conf01-sce03-s0405.t119.rss-nomove.v1'
CONFIG_SCHEMA='b03-runtime-rss-1'
OUTPUT_SCHEMA='b03.runtime.rss.v1'
SCENARIO='sce20261012-conf01/sce03'
SCRIPTS=('script-04.json', 'script-05.json')
PROFILE={'id':PROFILE_ID,'status':'development_assumption','link_direction':'undirected',
 'capacity_sharing':'shared_per_link','priority_order':'higher_first','delay_aggregation':'sum',
 'jitter_aggregation':'root_sum_squares','ber_aggregation':'union_bound',
 'loss_aggregation':'independent_product','node_move_semantics':'unsupported_fail_closed'}
def public_identity():
 return {'schemaVersion':OUTPUT_SCHEMA,'semanticsId':SEMANTICS_ID,'capabilityId':CAPABILITY_ID,'profileId':PROFILE_ID}
