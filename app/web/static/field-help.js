(() => {
  const help = {
    email: "The email address associated with this account, sender, or contact.",
    password: "The password used to sign in to this private workspace.",
    name: "A recognizable name used to identify this saved item in the app.",
    truck_profile_id: "The saved truck whose location, equipment, and carrier facts apply to this load.",
    mission_id: "The saved route and rate rules the agent must follow for this negotiation.",
    broker_email: "The broker address that will receive the load inquiry and all replies in this thread.",
    subject: "The email subject brokers see; leave it blank to use the selected template subject.",
    origin_city: "The city where the truck or broker load begins.",
    origin_state: "The two-letter state where the truck or broker load begins.",
    current_city: "The city where this truck is currently available or normally based.",
    current_state: "The two-letter state where this truck is currently available or based.",
    origin_deadhead_miles: "The empty miles you are willing to travel from the truck location to pickup.",
    pickup_start: "The first date this truck is available to pick up a load.",
    pickup_end: "The last date this truck is available within this mission.",
    pickup_date: "The broker's confirmed pickup date used to check the mission availability window.",
    equipment_type: "The trailer or equipment type the agent uses to answer compatibility questions.",
    trailer_length_ft: "The trailer length in feet that the agent may quote to brokers.",
    max_weight_lbs: "The maximum cargo weight this truck can safely accept.",
    team_status: "Whether this is a solo truck or a true team, used when a broker has a transit requirement.",
    destination_label: "A city, state, region, or open area this mission is willing to deliver to.",
    destination_kind: "How the destination text should be interpreted when checking a broker's route.",
    destination_radius: "The acceptable distance in miles around a destination city; use zero for an exact match.",
    destination_city: "The broker's confirmed delivery city used to check this mission's destinations.",
    destination_state: "The broker's confirmed two-letter delivery state.",
    floor_total: "The lowest total payment the agent may consider for the load.",
    target_total: "The total payment the agent should try to reach during negotiation.",
    floor_loaded_rpm: "The lowest acceptable dollars per loaded mile, excluding deadhead.",
    floor_all_in_rpm: "The lowest acceptable dollars per mile after loaded and deadhead miles are combined.",
    target_all_in_rpm: "The all-in dollars per mile the agent should seek when calculating a counteroffer.",
    counter_amount: "The exact total dollar amount the agent should request when it sends a counteroffer.",
    maximum_counter_rounds: "The most counteroffers the agent may send before asking you to review the thread.",
    execution_mode: "Auto sends permitted replies within your rules, while Approve creates drafts for review.",
    mc_number: "The carrier's Motor Carrier number; initial emails require explicit confirmation before sharing it.",
    dot_number: "The carrier's USDOT identifier; initial emails require explicit confirmation before sharing it.",
    dispatcher_name: "The dispatcher name saved for contact and signature context.",
    dispatcher_phone: "The dispatcher phone number saved for broker contact when you choose to provide it.",
    shareable_fields: "Checked facts may be quoted automatically in broker replies without asking you first.",
    sender_name: "The sender name recipients see beside the email address.",
    sender_email: "The default email address used when a sender-specific address is unavailable.",
    sender_business: "The business name available to outreach templates and the copywriting agent.",
    sender_business_url: "The business website available to templates and generated outreach copy.",
    display_name: "The human-readable sender name recipients see in their inbox.",
    reply_to: "Replies go to this address; leave it blank to use the sending address.",
    default_sender_account: "The connected Gmail account used for freight inquiries and reply monitoring.",
    default_template_id: "The freight email template automatically selected for new load inquiries.",
    email_signature: "The signature inserted into messages that use the signature template variable.",
    signature: "The signature appended to messages sent from this sender identity.",
    app_password: "A Google App Password lets the app send and monitor mail without storing your Google password.",
    body: "The message content sent after its template variables are filled.",
    body_text: "The reply text that will be sent to the broker in this conversation.",
    angle_tag: "An internal label used to group templates by message angle or strategy.",
    type: "Whether the template is sent as plain text or formatted HTML.",
    business_context: "Facts the copywriting agent may use when drafting or improving outreach.",
    business_name: "The company name used to identify this lead.",
    short_name: "A shorter company name used in greetings; it is generated when left blank.",
    owner_first_name: "The contact name used to personalize outreach when known.",
    phone: "The contact phone number stored with this lead.",
    website: "The lead's website, used for identification and duplicate checking.",
    category: "The trade or business type used for targeting and template personalization.",
    city: "The city used to identify or target this contact.",
    state: "The two-letter state used to identify or target this contact.",
    zip: "The postal code stored with this lead.",
    outreach_angle: "Internal context describing the message approach for this lead.",
    notes: "Private context for your team that is not sent automatically.",
    product_name: "The product name available to outreach templates and generated copy.",
    product_url: "The product link available to outreach templates and generated copy.",
    booking_link: "The scheduling link inserted when a template asks the recipient to book time.",
    callback_number: "The phone number recipients can use to call your team.",
    client_count: "The customer count used as social proof in templates that reference it.",
    client_noun: "The word used after the customer count, such as clients or contractors.",
    categories: "The business types the discovery search should find.",
    locations: "The cities, states, or ZIP codes the discovery search should cover.",
    result_limit: "The maximum number of discovery results requested for each location.",
    source: "The lead source used to limit which contacts enter this campaign.",
    import_batch_id: "A specific uploaded list used to limit this campaign's audience.",
    status: "The lead status used to filter or update contacts.",
    template_ids: "The selected messages rotate across eligible campaign recipients.",
    provider: "The email service used to send messages from the selected identities.",
    gmail_accounts: "The sender identities allowed to send messages for this campaign.",
    file: "The CSV file containing leads you want to preview and import.",
    map__source: "The lead field that should receive values from this CSV column.",
    timezone: "The time zone used to calculate your configured sending window.",
    daily_cap: "The maximum Gmail messages each active sender may send per day.",
    send_start: "The earliest local time scheduled Gmail messages may send.",
    send_end: "The latest local time scheduled Gmail messages may send.",
    min_delay_minutes: "The shortest spacing allowed between scheduled messages.",
    max_delay_minutes: "The longest random spacing used between scheduled messages.",
    resend_block_days: "The number of days before the same contact can enter outreach again.",
    pingram_daily_cap: "The maximum Pingram messages each sender may send per day.",
    pingram_min_delay: "The shortest spacing between Pingram messages.",
    pingram_max_delay: "The longest random spacing between Pingram messages.",
    gmail_sender_id: "The connected sender used for this test email.",
    to: "The recipient address for this test email.",
    loaded_miles: "The broker's confirmed miles from pickup to delivery.",
    deadhead_miles: "The actual empty miles from this truck's location to pickup.",
    weight_lbs: "The broker's confirmed cargo weight used to check truck capacity.",
    schedule_confirmed: "Confirm that the broker's pickup and delivery timing works for this truck."
    ,active: "Turn this on when the saved item is complete and ready for the app to use."
    ,auto_select: "Choose whether this account should immediately become Freight's default sender."
    ,send_days: "The weekdays on which scheduled outreach is allowed to send."
    ,action: "The operation that will be applied to the selected records."
    ,lead_ids: "Select the leads that should receive the chosen bulk action."
    ,search: "Text used to narrow the records currently shown on this page."
  };

  const pathHelp = {
    "/freight/missions/save|name": "A short name that helps dispatchers recognize this route and negotiation plan.",
    "/freight/profiles/save|name": "A short name that identifies this truck or unit when starting an inquiry.",
    "/templates/save|name": "An internal name that helps your team recognize this email template.",
    "/scraper/presets|name": "A name used to find and reuse this discovery search later.",
    "/campaigns/save|name": "An internal name used to identify and report on this outreach campaign."
  };

  function sentenceFor(control) {
    const explicit = control.dataset.help;
    if (explicit) return explicit;
    const formPath = control.form?.getAttribute("action") || "";
    const name = control.name || "";
    const normalized = name.startsWith("map__") ? "map__source" : name;
    return pathHelp[`${formPath}|${name}`] || help[normalized] || "This value is saved with the form and used where this label appears.";
  }

  // Freight pages keep info icons only on fields that are hard to guess.
  // Anything else can opt in with a data-help attribute.
  const freightHelpFields = new Set(["origin_deadhead_miles", "deadhead_miles", "floor_loaded_rpm", "floor_all_in_rpm", "target_all_in_rpm", "maximum_counter_rounds", "app_password", "shareable_fields"]);
  const freightPage = document.body?.classList.contains("ws-freight");

  function decorate(control) {
    if (freightPage && !control.dataset.help && !freightHelpFields.has(control.name || "")) return;
    if (control.dataset.helpDecorated || control.type === "hidden" || control.type === "submit" || control.type === "button") return;
    if (control.matches("[type=checkbox], [type=radio]") && !control.closest("label.check")) return;
    const label = control.closest("label") || (control.id && document.querySelector(`label[for="${CSS.escape(control.id)}"]`));
    let anchor = label;
    if (!anchor) {
      if (control.parentElement?.classList.contains("input-with-unit")) {
        anchor = control.parentElement;
        anchor.classList.add("field-control-anchor");
      } else {
        anchor = document.createElement("span");
        anchor.className = "field-control-anchor";
        control.before(anchor);
        anchor.appendChild(control);
      }
    }
    if (anchor.querySelector(":scope > .field-info")) return;
    const text = sentenceFor(control);
    const icon = document.createElement("span");
    icon.className = "field-info";
    icon.tabIndex = 0;
    icon.setAttribute("role", "note");
    icon.setAttribute("aria-label", text);
    icon.dataset.tip = text;
    icon.textContent = "i";
    anchor.appendChild(icon);
    if (control.required && !anchor.querySelector(":scope > .field-required-mark")) {
      const mark = document.createElement("span");
      mark.className = "field-required-mark";
      mark.setAttribute("aria-hidden", "true");
      mark.textContent = "*";
      anchor.appendChild(mark);
    }
    control.dataset.helpDecorated = "true";
  }

  function decorateAll(root = document) {
    root.querySelectorAll("form input, form select, form textarea").forEach(decorate);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", () => decorateAll());
  else decorateAll();

  new MutationObserver(records => records.forEach(record => record.addedNodes.forEach(node => {
    if (!(node instanceof Element)) return;
    if (node.matches("input, select, textarea")) decorate(node);
    decorateAll(node);
  }))).observe(document.documentElement, { childList: true, subtree: true });
})();
