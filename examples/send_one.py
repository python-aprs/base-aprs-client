import aprs

with aprs.TCP(
    host="localhost",
    port=14588,
    user="KF7HVM",
    passcode="-1",  # use a real passcode for TX
    command='filter r/46.1/-122.9/500',
) as aprs_tcp:
    frame = aprs.APRSFrame.from_str('KF7HVM-2>APRS:>Test from aprs!')
    aprs_tcp.write(frame)