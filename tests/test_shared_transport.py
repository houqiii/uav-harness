import asyncio
import socket
import pytest
from uav_harness.config import Settings
from uav_harness.transport import MAVLinkTransport, mav


async def test_one_modem_link_addresses_heterogeneous_systems_independently(tmp_path):
    wire=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    wire.setblocking(False);wire.bind(("127.0.0.1",0))
    settings=Settings(state_dir=tmp_path,ack_timeout_s=.5,
                      links=[{"name":"radio","kind":"udp","peer_port":wire.getsockname()[1]}],
                      vehicles=[{"vehicle_id":"ap","link":"radio","backend":"ardupilot","system_id":1},
                                {"vehicle_id":"px4","link":"radio","backend":"px4","system_id":2}])
    transport=MAVLinkTransport(settings.links[0],settings,{(1,1):3,(2,1):12})
    received=[]
    class Output:
        def __init__(self,destination):self.destination=destination
        def write(self,data):wire.sendto(data,self.destination)
    async def responder():
        parser=mav.MAVLink(None)
        while len(received)<2:
            data,peer=await asyncio.get_running_loop().sock_recvfrom(wire,65535)
            for message in parser.parse_buffer(data) or []:
                if message.get_type()=="COMMAND_LONG":
                    received.append(message.to_dict())
                    tx=mav.MAVLink(Output(peer),srcSystem=message.target_system,srcComponent=1)
                    tx.command_ack_send(message.command,0,target_system=message.get_srcSystem(),target_component=message.get_srcComponent())
    await transport.open()
    reply=asyncio.create_task(responder())
    try:
        results=await asyncio.gather(transport.command(1,1,22,[0,0,0,0,0,0,2]),
                                     transport.command(2,1,400,[1,0]))
        await reply
        assert all(r["result"]=="ACCEPTED" for r in results)
        assert {(r["target_system"],r["command"]) for r in received}=={(1,22),(2,400)}
    finally:
        reply.cancel();await asyncio.gather(reply,return_exceptions=True)
        await transport.close();wire.close()


async def test_unrelated_system_heartbeat_cannot_replace_vehicle_state(harness):
    peer=harness.simulators["ap-1"];s=harness.sessions["ap-1"]
    peer.stop_telemetry=True
    previous=s.latest["HEARTBEAT"]
    tx=mav.MAVLink(None,srcSystem=99,srcComponent=1)
    packet=tx.heartbeat_encode(2,12,209,6<<16,4).pack(tx)
    peer.sock.sendto(packet,peer.destination)
    await asyncio.sleep(.1)
    assert s.latest["HEARTBEAT"]==previous and s.fault is None

