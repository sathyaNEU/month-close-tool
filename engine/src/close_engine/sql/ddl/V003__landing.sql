-- V003__landing.sql: The expected landing shapes the generator writes and the default data sources read.
-- Generated once from close_contracts.table_schemas; from here on it is history and never edited.

CREATE TABLE IF NOT EXISTS {landing__sellers} (
  seller_id STRING NOT NULL,
  entity_id STRING NOT NULL,
  country STRING NOT NULL,
  level INT NOT NULL COMMENT 'seller level, which sets the fee tier',
  fee_bps INT NOT NULL,
  payout_provider STRING NOT NULL COMMENT 'hyperwallet or paypal',
  created_at TIMESTAMP NOT NULL
) USING DELTA;

CREATE TABLE IF NOT EXISTS {landing__orders} (
  order_id STRING NOT NULL,
  entity_id STRING NOT NULL,
  program STRING NOT NULL,
  buyer_id STRING NOT NULL,
  seller_id STRING NOT NULL,
  sku STRING NOT NULL,
  currency STRING NOT NULL COMMENT 'presentment currency',
  item_price_minor BIGINT NOT NULL,
  buyer_fee_minor BIGINT NOT NULL,
  shipping_minor BIGINT NOT NULL,
  tax_minor BIGINT NOT NULL,
  incentive_minor BIGINT NOT NULL,
  store_credit_minor BIGINT NOT NULL COMMENT 'split tender: paid with store credit',
  card_amount_minor BIGINT NOT NULL COMMENT 'charged through the payment provider',
  seller_fee_minor BIGINT NOT NULL,
  seller_processing_fee_minor BIGINT NOT NULL,
  psp STRING NOT NULL COMMENT 'stripe or adyen',
  psp_payment_ref STRING COMMENT 'PaymentIntent id or Adyen merchant reference',
  placed_at TIMESTAMP NOT NULL,
  status STRING NOT NULL
) USING DELTA;

CREATE TABLE IF NOT EXISTS {landing__order_events} (
  event_id STRING NOT NULL,
  order_id STRING NOT NULL,
  entity_id STRING NOT NULL,
  event_type STRING NOT NULL,
  occurred_at TIMESTAMP NOT NULL,
  tracking_number STRING,
  amount_minor BIGINT COMMENT 'refunds and penalties',
  refund_method STRING COMMENT 'card or store_credit'
) USING DELTA;

CREATE TABLE IF NOT EXISTS {landing__shipping_labels} (
  label_id STRING NOT NULL,
  order_id STRING NOT NULL,
  entity_id STRING NOT NULL,
  carrier STRING NOT NULL,
  tracking_number STRING NOT NULL,
  leg STRING NOT NULL COMMENT 'seller_to_authentication, authentication_to_buyer, seller_to_buyer, return_to_seller',
  service STRING NOT NULL,
  quoted_minor BIGINT NOT NULL,
  currency STRING NOT NULL,
  weight_grams INT NOT NULL,
  ship_date DATE NOT NULL,
  purchased_at TIMESTAMP NOT NULL
) USING DELTA;

CREATE TABLE IF NOT EXISTS {landing__carrier_invoice_lines} (
  invoice_id STRING NOT NULL,
  line_id STRING NOT NULL,
  carrier STRING NOT NULL,
  account_number STRING NOT NULL,
  tracking_number STRING NOT NULL,
  ship_date DATE NOT NULL,
  service STRING NOT NULL,
  charge_type STRING NOT NULL COMMENT 'transportation, fuel_surcharge, address_correction, weight_adjustment',
  billed_weight_grams INT NOT NULL,
  billed_minor BIGINT NOT NULL,
  currency STRING NOT NULL,
  invoice_date DATE NOT NULL
) USING DELTA;

CREATE TABLE IF NOT EXISTS {landing__stripe_balance_transactions} (
  id STRING NOT NULL COMMENT 'txn_...',
  account_id STRING NOT NULL,
  type STRING NOT NULL COMMENT 'charge, refund, adjustment, payout, stripe_fee, dispute',
  reporting_category STRING NOT NULL,
  source_id STRING COMMENT 'ch_, re_, dp_ or po_ object',
  payment_intent_id STRING,
  order_ref STRING COMMENT 'metadata[order_id]',
  amount BIGINT NOT NULL COMMENT 'settlement currency, minor units, signed',
  fee BIGINT NOT NULL,
  net BIGINT NOT NULL,
  currency STRING NOT NULL COMMENT 'lower case, as Stripe reports it',
  customer_facing_amount BIGINT COMMENT 'what the buyer was charged, presentment currency',
  customer_facing_currency STRING,
  created TIMESTAMP NOT NULL,
  available_on DATE NOT NULL,
  automatic_payout_id STRING
) USING DELTA;

CREATE TABLE IF NOT EXISTS {landing__stripe_payouts} (
  id STRING NOT NULL COMMENT 'po_...',
  account_id STRING NOT NULL,
  amount BIGINT NOT NULL,
  currency STRING NOT NULL,
  arrival_date DATE NOT NULL,
  created TIMESTAMP NOT NULL,
  status STRING NOT NULL COMMENT 'paid, failed, canceled',
  trace_id STRING,
  destination STRING NOT NULL COMMENT 'the bank account'
) USING DELTA;

CREATE TABLE IF NOT EXISTS {landing__adyen_settlement_details} (
  company_account STRING NOT NULL,
  merchant_account STRING NOT NULL,
  psp_reference STRING,
  merchant_reference STRING,
  payment_method STRING,
  creation_date TIMESTAMP NOT NULL,
  type STRING NOT NULL COMMENT 'Settled, Refunded, Chargeback, ChargebackReversed, MerchantPayout, InvoiceDeduction, Fee',
  modification_reference STRING,
  gross_currency STRING NOT NULL,
  gross_debit STRING COMMENT 'decimal text, as in the report',
  gross_credit STRING,
  net_currency STRING NOT NULL,
  net_debit STRING,
  net_credit STRING,
  commission STRING,
  markup STRING,
  scheme_fees STRING,
  interchange STRING,
  batch_number INT NOT NULL
) USING DELTA;

CREATE TABLE IF NOT EXISTS {landing__psp_fee_invoices} (
  invoice_id STRING NOT NULL,
  psp STRING NOT NULL,
  account_id STRING NOT NULL,
  period STRING NOT NULL,
  amount_minor BIGINT NOT NULL,
  currency STRING NOT NULL,
  invoice_date DATE NOT NULL
) USING DELTA;

CREATE TABLE IF NOT EXISTS {landing__disputes} (
  dispute_id STRING NOT NULL,
  psp STRING NOT NULL,
  charge_ref STRING NOT NULL,
  order_id STRING NOT NULL,
  entity_id STRING NOT NULL,
  status STRING NOT NULL COMMENT 'needs_response, under_review, won, lost',
  reason STRING NOT NULL,
  amount_minor BIGINT NOT NULL,
  fee_minor BIGINT NOT NULL,
  currency STRING NOT NULL,
  opened_at TIMESTAMP NOT NULL,
  closed_at TIMESTAMP
) USING DELTA;

CREATE TABLE IF NOT EXISTS {landing__seller_payables} (
  payable_id STRING NOT NULL,
  seller_id STRING NOT NULL,
  order_id STRING,
  entity_id STRING NOT NULL,
  program STRING NOT NULL,
  type STRING NOT NULL COMMENT 'sale, penalty, clawback, adjustment',
  amount_minor BIGINT NOT NULL COMMENT 'net proceeds; negative for penalties and clawbacks',
  currency STRING NOT NULL,
  created_at TIMESTAMP NOT NULL,
  transfer_group STRING NOT NULL COMMENT 'the payout reference sent to the payout provider'
) USING DELTA;

CREATE TABLE IF NOT EXISTS {landing__payout_items} (
  provider STRING NOT NULL COMMENT 'hyperwallet or paypal',
  payment_id STRING NOT NULL,
  client_payment_id STRING NOT NULL COMMENT 'our transfer_group (Hyperwallet clientPaymentId, PayPal sender_item_id)',
  batch_id STRING NOT NULL,
  seller_id STRING NOT NULL,
  entity_id STRING NOT NULL,
  amount STRING NOT NULL COMMENT 'decimal text, as the providers report it',
  currency STRING NOT NULL,
  status STRING NOT NULL COMMENT 'COMPLETED, FAILED, RETURNED, UNCLAIMED',
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL
) USING DELTA;

CREATE TABLE IF NOT EXISTS {landing__payout_funding} (
  provider STRING NOT NULL,
  funding_id STRING NOT NULL,
  entity_id STRING NOT NULL,
  amount STRING NOT NULL,
  currency STRING NOT NULL,
  funded_at TIMESTAMP NOT NULL,
  value_date DATE NOT NULL,
  bank_reference STRING NOT NULL
) USING DELTA;

CREATE TABLE IF NOT EXISTS {landing__bank_statement_lines} (
  statement_id STRING NOT NULL,
  line_id STRING NOT NULL,
  account_id STRING NOT NULL,
  bank STRING NOT NULL,
  format STRING NOT NULL COMMENT 'bai2, camt053 or mt940',
  booking_date DATE NOT NULL,
  value_date DATE NOT NULL,
  amount_text STRING NOT NULL COMMENT 'raw: BAI2 implied decimals, MT940 decimal comma, camt.053 decimal',
  credit_debit STRING NOT NULL COMMENT 'C or D',
  currency STRING NOT NULL,
  type_code STRING COMMENT 'BAI2 type code or camt.053 bank transaction code',
  bank_reference STRING COMMENT 'BAI2 bank reference, camt AcctSvcrRef, MT940 :61: bank reference',
  customer_reference STRING,
  end_to_end_id STRING,
  text STRING COMMENT 'BAI2 88 records, camt RmtInf, MT940 :86:'
) USING DELTA;

CREATE TABLE IF NOT EXISTS {landing__tax_documents} (
  document_code STRING NOT NULL COMMENT 'the order id',
  document_type STRING NOT NULL COMMENT 'SalesInvoice or ReturnInvoice',
  status STRING NOT NULL COMMENT 'Committed, Uncommitted or Voided',
  entity_id STRING NOT NULL,
  document_date DATE NOT NULL,
  currency STRING NOT NULL,
  total_amount STRING NOT NULL COMMENT 'decimal text, as AvaTax reports it',
  total_tax STRING NOT NULL,
  jurisdiction STRING NOT NULL,
  committed_at TIMESTAMP
) USING DELTA;

CREATE TABLE IF NOT EXISTS {landing__tax_remittances} (
  remittance_id STRING NOT NULL,
  entity_id STRING NOT NULL,
  jurisdiction STRING NOT NULL,
  period STRING NOT NULL,
  amount_minor BIGINT NOT NULL,
  currency STRING NOT NULL,
  paid_date DATE NOT NULL,
  bank_reference STRING NOT NULL
) USING DELTA;

CREATE TABLE IF NOT EXISTS {landing__stored_value_ledger} (
  entry_id STRING NOT NULL,
  account_id STRING NOT NULL COMMENT 'the buyer\'s stored-value account',
  entity_id STRING NOT NULL,
  type STRING NOT NULL COMMENT 'issued, redeemed, expired, breakage, escheat',
  source STRING NOT NULL COMMENT 'refund, gift_card_sale, promotion, checkout',
  order_id STRING,
  amount_minor BIGINT NOT NULL COMMENT 'positive when issued, negative when used or removed',
  currency STRING NOT NULL,
  occurred_at TIMESTAMP NOT NULL
) USING DELTA;
