ALTER TABLE gmail_senders ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'gmail';

DELETE FROM gmail_senders WHERE provider = 'pingram';

INSERT INTO gmail_senders (id, email, display_name, signature, reply_to, app_password_encrypted, active, provider) VALUES
(gen_random_uuid(), 'yaritza@contractorops.ai', 'Yaritza', 'Yaritza\nPartnerships @ ContractorOps', 'outreach@contractorops.ai', '', true, 'pingram'),
(gen_random_uuid(), 'mariana@contractorops.ai', 'Mariana', 'Mariana\nGrowth Team @ ContractorOps', 'outreach@contractorops.ai', '', true, 'pingram'),
(gen_random_uuid(), 'nancy@contractorops.ai', 'Nancy', 'Nancy\nOutreach Specialist @ ContractorOps', 'outreach@contractorops.ai', '', true, 'pingram'),
(gen_random_uuid(), 'sofia@contractorops.ai', 'Sofia', 'Sofia\nContractor Success @ ContractorOps', 'outreach@contractorops.ai', '', true, 'pingram'),
(gen_random_uuid(), 'sarah@contractorops.ai', 'Sarah', 'Sarah\nAccount Executive @ ContractorOps', 'outreach@contractorops.ai', '', true, 'pingram'),
(gen_random_uuid(), 'emily@contractorops.ai', 'Emily', 'Emily\nMarketing Manager @ ContractorOps', 'outreach@contractorops.ai', '', true, 'pingram'),
(gen_random_uuid(), 'jane@contractorops.ai', 'Jane', 'Jane\nGrowth @ ContractorOps', 'outreach@contractorops.ai', '', true, 'pingram'),
(gen_random_uuid(), 'bekah@contractorops.ai', 'Bekah', 'Bekah\nSales @ ContractorOps', 'outreach@contractorops.ai', '', true, 'pingram'),
(gen_random_uuid(), 'jessica@contractorops.ai', 'Jessica', 'Jessica\nClient Partnerships @ ContractorOps', 'outreach@contractorops.ai', '', true, 'pingram'),
(gen_random_uuid(), 'olivia@contractorops.ai', 'Olivia', 'Olivia\nOperations & Outreach @ ContractorOps', 'outreach@contractorops.ai', '', true, 'pingram');
