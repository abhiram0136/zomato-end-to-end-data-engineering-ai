select
    order_item_id::number as order_item_id,
    order_id::number as order_id,
    r_id::number as restaurant_id,
    f_id::varchar as f_id,
    price::number as price,
    quantity::number as quantity,
    line_amount::number as line_amount
from {{ source('raw', 'order_items') }}
